"""Event-driven intraday backtester for the 9/21 EMA crossover on NIFTY options.

Explicit execution rules (also shown in the UI "Rules" panel)
-------------------------------------------------------------
1. Signals come from CLOSED spot bars (see indicators.py). The signal time T is
   the bar's close time (bar_end).
2. Fill bar: every order fills at the OPEN of the option's 1-minute candle that
   starts at T (the first minute after the signal bar closes). If that minute is
   missing, the first candle within the next 5 minutes is used (flag
   ``fill_delayed``). If none exists the entry is skipped (logged with reason);
   for an exit the last close before T is used (flag ``stale_exit``).
3. Entry window: a cross can OPEN a trade only if start <= T <= cutoff
   (both inclusive). Crosses outside are ignored for entries.
4. Exit: the first opposite cross at any time before 15:00 (even after the
   cutoff), else mandatory square-off at the 15:00 candle open.
5. Immediate reversal: an opposite cross INSIDE the window closes the trade and
   opens the reverse trade at the same fill minute.
6. A signal with T >= 15:00 never opens a trade; open trades are squared off at 15:00.
7. One position per mode at a time; each mode (ITM5 buy / ATM buy / ATM sell) is
   simulated independently on the same signals.
8. Contract: strike from the signal bar CLOSE; nearest weekly expiry on or after
   the trade date (expiry-day trades use the 0-DTE contract).
9. No-trade days: days with no spot data, no closed in-window cross, or whose
   every entry was skipped are listed with a reason.
10. Whipsaw: trade closed by an opposite cross within 2 signal bars of entry.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from ..config import (DEFAULT_CAPITAL, DEFAULT_FRICTION, LOT_SIZE, MODES, SQUARE_OFF,
                      FrictionConfig)
from .contracts import Contract, nearest_expiry, option_side, select_contract
from .costs import round_trip
from .indicators import add_emas, at_time, detect_crosses, resample
from .metrics import compute_metrics
from .rules import actions_for_cross

FILL_GRACE_MIN = 5
WHIPSAW_BARS = 2
ENGINE_VERSION = "1.0"


@dataclass(frozen=True)
class BacktestParams:
    start_date: date
    end_date: date
    timeframe: int = 15
    entry_start: time = time(9, 30)
    entry_cutoff: time = time(14, 30)
    lots: int = 1
    lot_size: int = LOT_SIZE
    modes: tuple = MODES
    capital: float = DEFAULT_CAPITAL
    friction: FrictionConfig = field(default_factory=lambda: DEFAULT_FRICTION)

    def __post_init__(self):
        if self.timeframe not in (5, 15, 30):
            raise ValueError("timeframe must be 5, 15 or 30")
        if self.entry_start >= self.entry_cutoff:
            raise ValueError("entry start must be before cutoff")
        if self.entry_cutoff > SQUARE_OFF:
            raise ValueError("cutoff must be at or before 15:00")
        if self.start_date > self.end_date:
            raise ValueError("start date after end date")
        if self.lots < 1:
            raise ValueError("lots must be >= 1")

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size

    def cache_key(self, data_version: str = "") -> str:
        d = asdict(self)
        d["v"] = ENGINE_VERSION
        d["data"] = data_version
        return hashlib.sha256(json.dumps(d, default=str, sort_keys=True).encode()).hexdigest()[:24]


def _fill(opt: pd.DataFrame, t: pd.Timestamp, *, is_exit: bool):
    """Return (price, actual_time, flag) or (None, None, reason)."""
    if opt is None or opt.empty:
        return None, None, "no option data"
    if t in opt.index:
        return float(opt.at[t, "open"]), t, None
    window = opt[(opt.index > t) & (opt.index <= t + timedelta(minutes=FILL_GRACE_MIN))]
    if not window.empty:
        return float(window["open"].iloc[0]), window.index[0], "fill_delayed"
    if is_exit:
        before = opt[opt.index < t]
        if not before.empty:
            return float(before["close"].iloc[-1]), before.index[-1], "stale_exit"
    return None, None, "no option candle near fill time"


def _spot_at(spot_day: pd.DataFrame, t: pd.Timestamp, fallback: float) -> float:
    if t in spot_day.index:
        return float(spot_day.at[t, "open"])
    after = spot_day[spot_day.index >= t]
    return float(after["open"].iloc[0]) if not after.empty else float(fallback)


class Backtester:
    def __init__(self, store, params: BacktestParams):
        self.store = store
        self.p = params
        self._opt_cache: dict = {}

    # -- data ---------------------------------------------------------------
    def _option(self, contract: Contract, day: date) -> pd.DataFrame:
        key = (contract.groww_symbol, day)
        if key not in self._opt_cache:
            self._opt_cache[key] = self.store.option_1m(contract.groww_symbol, day)
        return self._opt_cache[key]

    # -- main ---------------------------------------------------------------
    def run(self) -> dict:
        p = self.p
        warm_from = p.start_date - timedelta(days=14)
        spot = self.store.spot_1m(warm_from, p.end_date)
        expiries = sorted(self.store.expiries())
        bars = add_emas(resample(spot, p.timeframe))
        crosses = detect_crosses(bars)

        trading_days = sorted({d for d in spot.index.date if p.start_date <= d <= p.end_date}) if not spot.empty else []
        trades: list[dict] = []
        no_trade: list[dict] = []
        skipped: list[dict] = []

        cross_by_day = {d: g for d, g in crosses.groupby("day")} if not crosses.empty else {}
        spot_by_day = {d: g for d, g in spot.groupby(spot.index.date)} if not spot.empty else {}

        for day in trading_days:
            day_crosses = cross_by_day.get(day)
            spot_day = spot_by_day[day]
            if day_crosses is None or day_crosses.empty:
                no_trade.append({"day": day.isoformat(), "reason": "No 9/21 crossover today"})
                continue
            win_start = at_time(day, p.entry_start)
            win_end = at_time(day, p.entry_cutoff)
            sq = at_time(day, SQUARE_OFF)
            in_win = day_crosses[(day_crosses.signal_time >= win_start) & (day_crosses.signal_time <= win_end)]
            if in_win.empty:
                no_trade.append({"day": day.isoformat(),
                                 "reason": f"{len(day_crosses)} cross(es), none inside the entry window"})
                continue
            expiry = nearest_expiry(day, expiries)
            if expiry is None:
                no_trade.append({"day": day.isoformat(), "reason": "No expiry calendar for this date"})
                continue
            day_trades = 0
            for mode in p.modes:
                t, s = self._run_day_mode(day, mode, day_crosses, spot_day, expiry, win_start, win_end, sq)
                trades.extend(t)
                skipped.extend(s)
                day_trades += len(t)
            if day_trades == 0:
                no_trade.append({"day": day.isoformat(), "reason": "All entries skipped (option data missing)"})

        trades.sort(key=lambda r: (r["entry_time"], r["mode"]))
        for i, tr in enumerate(trades, 1):
            tr["id"] = i
        metrics = {m: compute_metrics([t for t in trades if t["mode"] == m], p.capital) for m in p.modes}
        return {
            "params": {
                "start_date": p.start_date.isoformat(), "end_date": p.end_date.isoformat(),
                "timeframe": p.timeframe, "entry_start": p.entry_start.strftime("%H:%M"),
                "entry_cutoff": p.entry_cutoff.strftime("%H:%M"), "lots": p.lots, "qty": p.qty,
            },
            "trading_days": len(trading_days),
            "trades": trades,
            "metrics": metrics,
            "no_trade_days": no_trade,
            "skipped": skipped,
        }

    def _run_day_mode(self, day, mode, day_crosses, spot_day, expiry, win_start, win_end, sq):
        p = self.p
        trades, skipped = [], []
        pos = None
        entry_no = 0
        for c in day_crosses.itertuples(index=False):
            t = pd.Timestamp(c.signal_time)
            if t >= sq:
                break
            in_window = win_start <= t <= win_end
            for act, arg in actions_for_cross(pos["direction"] if pos else None, int(c.direction), in_window):
                if act == "EXIT":
                    trades.append(self._close(pos, t, c, arg, spot_day))
                    pos = None
                else:
                    entry_no += 1
                    pos, why = self._open(day, mode, c, t, expiry, spot_day, entry_no)
                    if pos is None:
                        skipped.append({"day": day.isoformat(), "mode": mode, "time": t.strftime("%H:%M"),
                                        "reason": why})
        if pos is not None:
            trades.append(self._close(pos, sq, None, "SQUARE_OFF_15:00", spot_day))
        return trades, skipped

    def _open(self, day, mode, c, t, expiry, spot_day, entry_no):
        contract = select_contract(mode, int(c.direction), c.spot_close, expiry)
        opt = self._option(contract, day)
        px, ft, flag = _fill(opt, t, is_exit=False)
        if px is None:
            return None, f"{contract.label}: {flag}"
        return {
            "mode": mode, "direction": int(c.direction), "contract": contract,
            "entry_time": ft, "signal_time": t, "entry_raw": px,
            "spot_entry": _spot_at(spot_day, t, c.spot_close),
            "entry_cross_no": entry_no, "day_cross_no": int(c.day_cross_no),
            "flags": [flag] if flag else [], "day": day,
            "signal_bar": pd.Timestamp(c.bar_start),
        }, None

    def _close(self, pos, t, c, reason, spot_day):
        p = self.p
        contract: Contract = pos["contract"]
        opt = self._option(contract, pos["day"])
        px, ft, flag = _fill(opt, t, is_exit=True)
        flags = list(pos["flags"])
        if px is None:  # cannot happen if entry existed, but be safe
            px, ft, flag = pos["entry_raw"], t, "no_exit_price"
        if flag:
            flags.append(flag)
        is_short = option_side(pos["mode"]) == "SELL"
        rt = round_trip(pos["entry_raw"], px, p.qty, is_short, p.friction)
        fallback = c.spot_close if c is not None else float(spot_day["close"].iloc[-1])
        bars_held = (t - pos["signal_time"]) / timedelta(minutes=p.timeframe)
        exit_by_cross = reason in ("REVERSAL", "OPPOSITE_CROSS")
        return {
            "day": pos["day"].isoformat(),
            "mode": pos["mode"],
            "signal": "BULLISH" if pos["direction"] > 0 else "BEARISH",
            "side": "SELL" if is_short else "BUY",
            "symbol": contract.groww_symbol,
            "label": contract.label,
            "strike": contract.strike,
            "opt_type": contract.opt_type,
            "expiry": contract.expiry.isoformat(),
            "dte": (contract.expiry - pos["day"]).days,
            "signal_bar": pos["signal_bar"].isoformat(),
            "entry_time": pos["entry_time"].isoformat(),
            "exit_time": ft.isoformat(),
            "exit_signal_time": t.isoformat(),
            "spot_entry": round(pos["spot_entry"], 2),
            "spot_exit": round(_spot_at(spot_day, t, fallback), 2),
            "exit_reason": reason,
            "entry_cross_no": pos["entry_cross_no"],
            "day_cross_no": pos["day_cross_no"],
            "bars_held": round(bars_held, 2),
            "whipsaw": bool(exit_by_cross and bars_held <= WHIPSAW_BARS),
            "flags": flags,
            "timeframe": p.timeframe,
            **rt.to_dict(),
        }


def run_backtest(store, params: BacktestParams) -> dict:
    return Backtester(store, params).run()
