from datetime import date, datetime, time

import numpy as np
import pandas as pd
import pytest

from app.strategy.contracts import Contract, nearest_expiry, select_contract
from app.strategy.indicators import (add_emas, atm_strike, closed_bars, detect_crosses, ema, resample,
                                     signal_frame)
from app.strategy.rules import actions_for_cross

from .conftest import flat_ohlc, minute_index

D = date(2026, 3, 2)


def test_resample_alignment_30m():
    idx = minute_index(D)
    df = flat_ohlc(idx, np.arange(len(idx)))
    bars = resample(df, 30)
    assert bars.index[0] == pd.Timestamp("2026-03-02 09:15")
    assert bars.index[1] == pd.Timestamp("2026-03-02 09:45")
    assert bars.index[-1] == pd.Timestamp("2026-03-02 15:15")
    assert bars["n"].iloc[0] == 30 and bars["n"].iloc[-1] == 15       # last bar is partial
    assert bars["bar_end"].iloc[-1] == pd.Timestamp("2026-03-02 15:30")  # capped at close
    assert len(bars) == 13


@pytest.mark.parametrize("tf,count", [(5, 75), (15, 25), (30, 13)])
def test_resample_counts_and_ohlc(tf, count):
    idx = minute_index(D)
    vals = np.arange(len(idx), dtype=float)
    df = pd.DataFrame({"open": vals, "high": vals + 1, "low": vals - 1, "close": vals + 0.5, "volume": 1.0}, index=idx)
    bars = resample(df, tf)
    assert len(bars) == count
    first = bars.iloc[0]
    assert first.open == 0 and first.close == tf - 1 + 0.5
    assert first.high == tf - 1 + 1 and first.low == -1
    assert first.volume == tf


def test_resample_drops_out_of_session_minutes():
    idx = pd.date_range("2026-03-02 09:00", "2026-03-02 15:59", freq="1min")
    bars = resample(flat_ohlc(idx, np.ones(len(idx))), 15)
    assert bars.index.min().time() == time(9, 15)
    assert bars.index.max().time() == time(15, 15)


def test_ema_sma_seed_and_recursion():
    s = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    e = ema(s, 3)
    assert np.isnan(e[0]) and np.isnan(e[1])
    assert e[2] == pytest.approx(2.0)          # SMA of 1,2,3
    assert e[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)
    assert e[5] == pytest.approx(0.5 * 6 + 0.5 * (0.5 * 5 + 0.5 * 3.0))


def _bars_from_closes(closes, start="2026-03-02 09:15", tf=5):
    idx = pd.date_range(start, periods=len(closes), freq=f"{tf}min")
    b = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes}, index=idx, dtype=float)
    b["bar_end"] = b.index + pd.Timedelta(minutes=tf)
    return b


def _ref_crosses(diff):
    """Independent reference: indices where sign flips vs last non-zero sign."""
    out, last = [], 0
    for i, d in enumerate(diff):
        if np.isnan(d) or d == 0:
            continue
        s = np.sign(d)
        if last and s != last:
            out.append((i, int(s)))
        last = s
    return out


def test_detect_crosses_matches_reference_on_random_walk():
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0, 1, 600))
    bars = add_emas(_bars_from_closes(closes))
    got = detect_crosses(bars)
    ref = _ref_crosses((bars.ema9 - bars.ema21).to_numpy())
    assert [(bars.index.get_loc(r.bar_start), r.direction) for r in got.itertuples()] == ref
    assert len(ref) > 10
    # directions alternate
    dirs = got["direction"].tolist()
    assert all(a != b for a, b in zip(dirs, dirs[1:]))


def test_first_nonzero_sign_is_not_a_cross_and_ties_are_skipped():
    bars = _bars_from_closes([100.0] * 30)
    bars["ema9"] = [np.nan] * 20 + [0.0] * 10
    bars["ema21"] = [np.nan] * 20 + [0.0] * 10
    # diff: nan.. then 0, +1, 0, 0, -1, 0, +1
    diffs = [0, 1, 0, 0, -1, 0, 1, 0, 0, 0]
    bars.loc[bars.index[20:], "ema9"] = diffs
    c = detect_crosses(bars)
    # +1 at idx21 is the first non-zero sign -> no cross; -1 at idx24 -> bearish; +1 at idx26 -> bullish
    assert [bars.index.get_loc(t) for t in c.bar_start] == [24, 26]
    assert c.direction.tolist() == [-1, 1]
    assert c.signal_time.iloc[0] == bars.index[24] + pd.Timedelta(minutes=5)


def test_day_cross_numbering_resets_each_day():
    rng = np.random.default_rng(3)
    idx = minute_index(D).append(minute_index(date(2026, 3, 3)))
    vals = 100 + np.cumsum(rng.normal(0, 0.6, len(idx)))
    _, crosses = signal_frame(flat_ohlc(idx, vals), 5)
    for day, g in crosses.groupby("day"):
        assert g.day_cross_no.tolist() == list(range(1, len(g) + 1))


def test_closed_bars_excludes_forming_bar():
    idx = minute_index(D)
    bars = resample(flat_ohlc(idx, np.ones(len(idx))), 15)
    now = datetime(2026, 3, 2, 9, 44, 59)
    cb = closed_bars(bars, now)
    assert cb.index[-1] == pd.Timestamp("2026-03-02 09:15")  # 09:30 bar ends 09:45 -> still forming
    cb = closed_bars(bars, datetime(2026, 3, 2, 9, 45))
    assert cb.index[-1] == pd.Timestamp("2026-03-02 09:30")


def test_atm_strike_rounding():
    assert atm_strike(25024.99, 50) == 25000
    assert atm_strike(25025.0, 50) == 25050     # half rounds up
    assert atm_strike(25074.0, 50) == 25050
    assert atm_strike(25076.0, 50) == 25100


def test_select_contract_all_modes():
    e = date(2026, 3, 3)
    assert select_contract("ATM_BUY", 1, 25010, e) == Contract(e, 25000, "CE")
    assert select_contract("ATM_BUY", -1, 25010, e) == Contract(e, 25000, "PE")
    assert select_contract("ITM5_BUY", 1, 25010, e) == Contract(e, 24750, "CE")
    assert select_contract("ITM5_BUY", -1, 25010, e) == Contract(e, 25250, "PE")
    assert select_contract("ATM_SELL", 1, 25010, e) == Contract(e, 25000, "PE")
    assert select_contract("ATM_SELL", -1, 25010, e) == Contract(e, 25000, "CE")
    with pytest.raises(ValueError):
        select_contract("FOO", 1, 25000, e)


def test_groww_symbol_format():
    assert Contract(date(2025, 9, 30), 24650, "PE").groww_symbol == "NSE-NIFTY-30Sep25-24650-PE"


def test_nearest_expiry_includes_same_day():
    exps = [date(2026, 3, 3), date(2026, 3, 10)]
    assert nearest_expiry(date(2026, 3, 2), exps) == date(2026, 3, 3)
    assert nearest_expiry(date(2026, 3, 3), exps) == date(2026, 3, 3)   # 0-DTE
    assert nearest_expiry(date(2026, 3, 4), exps) == date(2026, 3, 10)
    assert nearest_expiry(date(2026, 3, 11), exps) is None


@pytest.mark.parametrize("pos,cross,win,expected", [
    (None, 1, True, [("ENTER", 1)]),
    (None, 1, False, []),
    (1, -1, True, [("EXIT", "REVERSAL"), ("ENTER", -1)]),
    (1, -1, False, [("EXIT", "OPPOSITE_CROSS")]),
    (-1, 1, True, [("EXIT", "REVERSAL"), ("ENTER", 1)]),
    (1, 1, True, []),   # same direction: no pyramiding
])
def test_rules_state_machine(pos, cross, win, expected):
    assert actions_for_cross(pos, cross, win) == expected
