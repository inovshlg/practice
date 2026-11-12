"""Backtest engine scenarios with fabricated crossovers (full control over timing)."""
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

import app.strategy.backtest as bt
from app.strategy.backtest import BacktestParams, run_backtest
from app.strategy.costs import round_trip

from .conftest import MemStore, flat_ohlc, minute_index

D1 = date(2026, 3, 2)   # Monday
D2 = date(2026, 3, 3)   # Tuesday (expiry)
EXP = [D2, date(2026, 3, 10)]


def opt_price(ts: pd.Timestamp) -> float:
    return 100.0 + (ts.hour * 60 + ts.minute - 555)  # 09:15 -> 100, +1 per minute


def option_df(day, drop=()):
    idx = minute_index(day)
    idx = idx[~idx.isin([pd.Timestamp(datetime.combine(day, t)) for t in drop])]
    o = np.array([opt_price(t) for t in idx])
    return pd.DataFrame({"open": o, "high": o + 1, "low": o - 1, "close": o + 0.5}, index=idx)


def make_store(days=(D1,), drop=None):
    spot = pd.concat([flat_ohlc(minute_index(d), np.full(375, 25010.0)) for d in days])
    st = MemStore(spot, expiries=EXP)
    st.default_option = lambda sym, day: option_df(day, (drop or {}).get(sym, ()))
    return st


def X(day, hhmm, direction, spot=25010.0, tf=15, n=1):
    t = pd.Timestamp(datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time()))
    return {"bar_start": t - pd.Timedelta(minutes=tf), "signal_time": t, "direction": direction, "spot_close": spot,
            "ema9": 0.0, "ema21": 0.0, "day": day, "day_cross_no": n}


@pytest.fixture
def crosses(monkeypatch):
    holder = {"rows": []}
    monkeypatch.setattr(bt, "detect_crosses", lambda bars: pd.DataFrame(
        holder["rows"], columns=["bar_start", "signal_time", "direction", "spot_close", "ema9", "ema21", "day",
                                 "day_cross_no"]))
    return holder


def P(**kw):
    base = dict(start_date=D1, end_date=D1, timeframe=15, entry_start=time(9, 30), entry_cutoff=time(14, 30),
                modes=("ATM_BUY",))
    base.update(kw)
    return BacktestParams(**base)


def test_entry_fills_at_open_of_minute_at_signal_time_and_squares_off(crosses):
    crosses["rows"] = [X(D1, "10:00", 1)]
    r = run_backtest(make_store(), P())
    assert len(r["trades"]) == 1
    t = r["trades"][0]
    assert t["entry_time"] == "2026-03-02T10:00:00"
    assert t["entry_raw"] == opt_price(pd.Timestamp("2026-03-02 10:00"))
    assert t["exit_time"] == "2026-03-02T15:00:00" and t["exit_reason"] == "SQUARE_OFF_15:00"
    assert t["exit_raw"] == opt_price(pd.Timestamp("2026-03-02 15:00"))
    assert t["symbol"] == "NSE-NIFTY-03Mar26-25000-CE" and t["dte"] == 1
    rt = round_trip(t["entry_raw"], t["exit_raw"], 65, False)
    assert t["net_pnl"] == round(rt.net_pnl, 2)


@pytest.mark.parametrize("hhmm,trades", [("09:15", 0), ("09:29", 0), ("09:30", 1), ("14:30", 1), ("14:31", 0)])
def test_entry_window_is_inclusive(crosses, hhmm, trades):
    crosses["rows"] = [X(D1, hhmm, 1)]
    r = run_backtest(make_store(), P())
    assert len(r["trades"]) == trades
    if not trades:
        assert r["no_trade_days"][0]["reason"].endswith("none inside the entry window")


def test_reversal_inside_window(crosses):
    crosses["rows"] = [X(D1, "10:00", 1, n=1), X(D1, "11:00", -1, n=2)]
    r = run_backtest(make_store(), P())
    a, b = r["trades"]
    assert a["signal"] == "BULLISH" and a["exit_reason"] == "REVERSAL" and a["exit_time"] == "2026-03-02T11:00:00"
    assert b["signal"] == "BEARISH" and b["opt_type"] == "PE" and b["entry_time"] == "2026-03-02T11:00:00"
    assert b["exit_reason"] == "SQUARE_OFF_15:00"
    assert (a["entry_cross_no"], b["entry_cross_no"]) == (1, 2)


def test_opposite_cross_after_cutoff_exits_without_reentry(crosses):
    crosses["rows"] = [X(D1, "14:00", 1), X(D1, "14:45", -1)]
    r = run_backtest(make_store(), P())
    assert len(r["trades"]) == 1
    assert r["trades"][0]["exit_reason"] == "OPPOSITE_CROSS"
    assert r["trades"][0]["exit_time"] == "2026-03-02T14:45:00"


def test_cross_before_window_is_ignored_and_later_cross_trades(crosses):
    crosses["rows"] = [X(D1, "09:15", -1, n=1), X(D1, "09:45", 1, n=2)]
    r = run_backtest(make_store(), P())
    assert len(r["trades"]) == 1
    t = r["trades"][0]
    assert t["signal"] == "BULLISH" and t["entry_cross_no"] == 1 and t["day_cross_no"] == 2


def test_signal_at_or_after_1500_never_trades(crosses):
    crosses["rows"] = [X(D1, "10:00", 1), X(D1, "15:00", -1)]
    r = run_backtest(make_store(), P(entry_cutoff=time(15, 0)))
    assert len(r["trades"]) == 1
    assert r["trades"][0]["exit_reason"] == "SQUARE_OFF_15:00"


def test_whipsaw_flag(crosses):
    crosses["rows"] = [X(D1, "10:00", 1), X(D1, "10:30", -1), X(D1, "12:00", 1)]
    r = run_backtest(make_store(), P())
    t1, t2, t3 = r["trades"]
    assert t1["bars_held"] == 2 and t1["whipsaw"] is True
    assert t2["bars_held"] == 6 and t2["whipsaw"] is False
    assert t3["whipsaw"] is False  # square-off is never a whipsaw


def test_missing_entry_candle_uses_next_within_5_min(crosses):
    sym = "NSE-NIFTY-03Mar26-25000-CE"
    crosses["rows"] = [X(D1, "10:00", 1)]
    r = run_backtest(make_store(drop={sym: (time(10, 0), time(10, 1))}), P())
    t = r["trades"][0]
    assert t["entry_time"] == "2026-03-02T10:02:00" and "fill_delayed" in t["flags"]


def test_entry_skipped_when_no_option_candle_nearby(crosses):
    sym = "NSE-NIFTY-03Mar26-25000-CE"
    gap = tuple(time(10, m) for m in range(0, 6))
    crosses["rows"] = [X(D1, "10:00", 1)]
    r = run_backtest(make_store(drop={sym: gap}), P())
    assert r["trades"] == []
    assert r["skipped"] and "no option candle" in r["skipped"][0]["reason"]
    assert r["no_trade_days"][0]["reason"].startswith("All entries skipped")


def test_stale_exit_uses_last_close(crosses):
    sym = "NSE-NIFTY-03Mar26-25000-CE"
    gap = tuple(time(h, m) for h in (14, 15) for m in range(60) if (h, m) >= (14, 55))
    crosses["rows"] = [X(D1, "10:00", 1)]
    r = run_backtest(make_store(drop={sym: gap}), P())
    t = r["trades"][0]
    assert "stale_exit" in t["flags"]
    assert t["exit_raw"] == opt_price(pd.Timestamp("2026-03-02 14:54")) + 0.5


def test_modes_trade_independently_on_same_signal(crosses):
    crosses["rows"] = [X(D1, "10:00", -1, spot=25010)]
    r = run_backtest(make_store(), P(modes=("ITM5_BUY", "ATM_BUY", "ATM_SELL")))
    by = {t["mode"]: t for t in r["trades"]}
    assert by["ITM5_BUY"]["strike"] == 25250 and by["ITM5_BUY"]["opt_type"] == "PE" and by["ITM5_BUY"]["side"] == "BUY"
    assert by["ATM_BUY"]["strike"] == 25000 and by["ATM_BUY"]["opt_type"] == "PE"
    assert by["ATM_SELL"]["strike"] == 25000 and by["ATM_SELL"]["opt_type"] == "CE" and by["ATM_SELL"]["side"] == "SELL"
    # option price rises by 1/min in this fixture: long gains, short loses, gross mirrors
    assert by["ATM_BUY"]["gross_pnl"] == -by["ATM_SELL"]["gross_pnl"] > 0


def test_expiry_day_trades_zero_dte(crosses):
    crosses["rows"] = [X(D2, "10:00", 1)]
    r = run_backtest(make_store(days=(D2,)), P(start_date=D2, end_date=D2))
    assert r["trades"][0]["dte"] == 0 and r["trades"][0]["expiry"] == "2026-03-03"


def test_no_cross_day_is_reported(crosses):
    crosses["rows"] = []
    r = run_backtest(make_store(days=(D1, D2)), P(end_date=D2))
    assert r["trading_days"] == 2
    assert [x["reason"] for x in r["no_trade_days"]] == ["No 9/21 crossover today"] * 2


def test_lots_scale_quantity(crosses):
    crosses["rows"] = [X(D1, "10:00", 1)]
    r = run_backtest(make_store(), P(lots=3))
    assert r["trades"][0]["qty"] == 195


@pytest.mark.parametrize("kw", [dict(timeframe=10), dict(entry_start=time(12), entry_cutoff=time(11)),
                                dict(entry_cutoff=time(15, 15)), dict(lots=0),
                                dict(start_date=D2, end_date=D1)])
def test_invalid_params(kw):
    with pytest.raises(ValueError):
        P(**kw)


def test_cache_key_changes_with_params_and_data():
    a = P()
    assert a.cache_key("v1") == P().cache_key("v1")
    assert a.cache_key("v1") != a.cache_key("v2")
    assert a.cache_key("v1") != P(timeframe=5).cache_key("v1")


def test_end_to_end_on_demo_market_is_consistent():
    """Real signal path (no monkeypatch) on the synthetic market: invariants must hold."""
    from app.data_loader import DemoSource
    src = DemoSource()
    d0, d1 = date(2026, 2, 2), date(2026, 3, 13)
    spot = src.spot(d0 - timedelta(days=14), d1)
    exps = src.expiries(d0, d1)
    st = MemStore(spot, expiries=exps)
    from app.strategy.contracts import Contract
    cache = {}

    def opt(sym, day):
        if (sym, day) not in cache:
            p = sym.split("-")
            cache[(sym, day)] = src.option(Contract(datetime.strptime(p[2], "%d%b%y").date(), int(p[3]), p[4]), day)
        return cache[(sym, day)]
    st.default_option = opt
    for tf in (5, 15, 30):
        r = run_backtest(st, P(start_date=d0, end_date=d1, timeframe=tf, modes=("ITM5_BUY", "ATM_BUY", "ATM_SELL")))
        assert r["trades"], tf
        for mode in ("ITM5_BUY", "ATM_BUY", "ATM_SELL"):
            ts = sorted((t for t in r["trades"] if t["mode"] == mode), key=lambda t: t["entry_time"])
            for a, b in zip(ts, ts[1:]):           # never overlapping positions
                assert a["exit_time"] <= b["entry_time"]
            for t in ts:
                assert "09:30" <= t["entry_time"][11:16] <= "14:30"
                assert t["exit_time"][11:16] <= "15:00"
                assert t["entry_time"] < t["exit_time"]
                assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["friction"], abs=0.02)
                assert t["friction"] > 0
            m = r["metrics"][mode]
            assert m["trades"] == len(ts)
            assert m["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in ts), abs=0.05)
