"""Resampling, EMA and crossover detection.

This module is the single source of truth for the signal; both the backtester and
the live engine call it, so a backtest and a live session see identical signals.

Explicit rules
--------------
* Input: 1-minute NIFTY spot candles, index = candle START time (IST, tz-naive).
  Only minutes in [09:15, 15:30) are used.
* Resampling: N-minute bars are aligned to the 09:15 open, i.e. bucket start =
  09:15 + floor((t - 09:15) / N) * N.  For 30m: 09:15, 09:45, ... 15:15.
  The last bar of the day can be shorter (15:15-15:30 for 30m); it is kept.
  A bar's ``bar_end`` = bar start + N minutes (capped at 15:30). The bar is
  CLOSED (usable for a signal) only once the clock reaches ``bar_end``.
* EMA: standard EMA (alpha = 2/(n+1)) on bar CLOSES, seeded with the SMA of the
  first n closes (TradingView convention). The EMA runs continuously across
  days (yesterday's bars warm up today's EMA) - exactly what a chart shows.
  Bars before the 21-EMA is defined never produce a signal.
* Crossover: diff = EMA9 - EMA21. A cross happens on the first closed bar where
  sign(diff) differs from the last NON-ZERO sign. Ties (diff == 0 exactly)
  never trigger a signal; they are skipped and the comparison carries over to
  the next bar.  +1 = bullish (9 crosses above 21), -1 = bearish.
* ``day_cross_no`` numbers every cross in a session from 09:15 (1, 2, 3 ...).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from ..config import SESSION_CLOSE, SESSION_OPEN

FAST, SLOW = 9, 21


def _minutes_since_open(ts: pd.DatetimeIndex) -> np.ndarray:
    return (ts.hour * 60 + ts.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)


def session_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only regular-session minutes."""
    if df.empty:
        return df
    t = df.index
    mins = t.hour * 60 + t.minute
    lo = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
    hi = SESSION_CLOSE.hour * 60 + SESSION_CLOSE.minute
    return df[(mins >= lo) & (mins < hi)]


def resample(df1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample 1m candles to ``minutes`` bars aligned to 09:15.

    Returns columns open, high, low, close, volume, n (number of 1m candles),
    bar_end; index = bar start.
    """
    cols = ["open", "high", "low", "close", "volume", "n", "bar_end"]
    if df1m.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], name="ts"))
    df = session_filter(df1m.sort_index())
    if df.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], name="ts"))
    offs = _minutes_since_open(df.index) // minutes * minutes
    starts = (df.index.normalize() + pd.Timedelta(hours=SESSION_OPEN.hour, minutes=SESSION_OPEN.minute)
              + pd.to_timedelta(offs, unit="m"))
    g = df.groupby(starts)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum() if "volume" in df else 0,
        "n": g["close"].count(),
    })
    out.index.name = "ts"
    day_close = out.index.normalize() + pd.Timedelta(hours=SESSION_CLOSE.hour, minutes=SESSION_CLOSE.minute)
    out["bar_end"] = np.minimum(out.index + pd.Timedelta(minutes=minutes), day_close)
    return out


def ema(values: pd.Series, n: int) -> pd.Series:
    """EMA seeded with the SMA of the first n values; NaN before that."""
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    if len(arr) < n:
        return pd.Series(out, index=values.index)
    alpha = 2.0 / (n + 1)
    out[n - 1] = arr[:n].mean()
    for i in range(n, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=values.index)


def add_emas(bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.copy()
    bars["ema9"] = ema(bars["close"], FAST)
    bars["ema21"] = ema(bars["close"], SLOW)
    return bars


def detect_crosses(bars: pd.DataFrame) -> pd.DataFrame:
    """Return one row per crossover (see module docstring for the rules).

    ``bars`` must already contain ema9 / ema21 and only CLOSED bars.
    """
    diff = (bars["ema9"] - bars["ema21"]).to_numpy()
    rows = []
    last_sign = 0
    day_counter: dict = {}
    for i, d in enumerate(diff):
        if np.isnan(d):
            continue
        s = 1 if d > 0 else (-1 if d < 0 else 0)
        if s == 0:
            continue
        if last_sign != 0 and s != last_sign:
            ts = bars.index[i]
            day = ts.date()
            day_counter[day] = day_counter.get(day, 0) + 1
            rows.append({
                "bar_start": ts,
                "signal_time": bars["bar_end"].iloc[i],
                "direction": s,
                "spot_close": float(bars["close"].iloc[i]),
                "ema9": float(bars["ema9"].iloc[i]),
                "ema21": float(bars["ema21"].iloc[i]),
                "day": day,
                "day_cross_no": day_counter[day],
            })
        last_sign = s
    return pd.DataFrame(rows, columns=["bar_start", "signal_time", "direction", "spot_close",
                                       "ema9", "ema21", "day", "day_cross_no"])


def closed_bars(bars: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Drop bars that are still forming at ``now`` (live use)."""
    return bars[bars["bar_end"] <= pd.Timestamp(now)]


def signal_frame(df1m: pd.DataFrame, minutes: int, now: datetime | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convenience: 1m candles -> (bars with EMAs, crosses)."""
    bars = resample(df1m, minutes)
    if now is not None:
        bars = closed_bars(bars, now)
    bars = add_emas(bars)
    return bars, detect_crosses(bars)


def atm_strike(spot: float, step: int) -> int:
    """Nearest strike; exact halves round UP (e.g. 25025 -> 25050 with step 50)."""
    return int(np.floor(spot / step + 0.5) * step)


def at_time(day, t: time) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, t))


def minutes_between(a: pd.Timestamp, b: pd.Timestamp) -> float:
    return (b - a) / timedelta(minutes=1)
