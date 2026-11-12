"""Download NIFTY spot + required option 1m candles into Postgres.

Pipeline
  1. Expiry calendar (Groww get_expiries) -> table ``expiries``.
  2. NIFTY spot 1m candles for every day in range -> ``candles``.
  3. Crossovers are computed for ALL timeframes (5/15/30) on the stored spot,
     and for every cross whose signal time is inside the widest possible entry
     window (09:30-14:30) the contracts any mode could trade are fetched for
     that day: ITM5 + ATM option to buy and the ATM option to sell.
  4. ``fetch_log`` remembers each (symbol, day) so re-runs only fetch what is new.

Sources implement ``expiries(d_from, d_to)``, ``spot(day_from, day_to)`` and
``option(contract, day)``.  ``GrowwSource`` hits the real API; ``DemoSource``
generates a reproducible synthetic market for demos / tests.
"""
from __future__ import annotations

import logging
import math
import threading
import traceback
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from .config import MODES, SPOT_GROWW_SYMBOL, TICK, TIMEFRAMES
from .db import Database
from .strategy.contracts import Contract, nearest_expiry, select_contract
from .strategy.indicators import add_emas, detect_crosses, resample

log = logging.getLogger(__name__)

WIDEST_START, WIDEST_CUTOFF = time(9, 30), time(14, 30)


def business_days(d_from: date, d_to: date) -> list[date]:
    out, d = [], d_from
    while d <= d_to:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
class GrowwSource:
    name = "groww"

    def __init__(self, client):
        self.client = client

    def expiries(self, d_from: date, d_to: date) -> list[date]:
        out = set()
        for y in range(d_from.year, d_to.year + 1):
            out.update(self.client.expiries(y))
        # include the next couple of months so the last days have an expiry
        nxt = d_to + timedelta(days=40)
        if nxt.year != d_to.year:
            out.update(self.client.expiries(nxt.year))
        return sorted(out)

    def spot(self, d_from: date, d_to: date) -> pd.DataFrame:
        return self.client.candles(SPOT_GROWW_SYMBOL, datetime.combine(d_from, time(9, 15)),
                                   datetime.combine(d_to, time(15, 30)), "CASH")

    def option(self, contract: Contract, day: date) -> pd.DataFrame:
        return self.client.candles(contract.groww_symbol, datetime.combine(day, time(9, 15)),
                                   datetime.combine(day, time(15, 30)), "FNO")


def _norm_cdf(x):
    return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))


def bs_price(spot, strike, t_years, vol, opt_type, r=0.065):
    spot = np.asarray(spot, dtype=float)
    t = np.maximum(np.asarray(t_years, dtype=float), 1e-6)
    d1 = (np.log(spot / strike) + (r + 0.5 * vol ** 2) * t) / (vol * np.sqrt(t))
    d2 = d1 - vol * np.sqrt(t)
    if opt_type == "CE":
        return spot * _norm_cdf(d1) - strike * np.exp(-r * t) * _norm_cdf(d2)
    return strike * np.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


class DemoSource:
    """Synthetic but realistic NIFTY market (GBM with trending regimes + BS options).

    Clearly labelled as DEMO in the UI - never confuse with real data.
    """
    name = "demo"

    def __init__(self, seed: int = 921, start_level: float = 25_000.0):
        self.seed = seed
        self.start_level = start_level
        self._spot_cache: dict[date, pd.DataFrame] = {}

    def expiries(self, d_from: date, d_to: date) -> list[date]:
        d = d_from - timedelta(days=d_from.weekday()) - timedelta(days=7)
        out = []
        while d <= d_to + timedelta(days=45):
            out.append(d + timedelta(days=(1 - d.weekday()) % 7))  # Tuesdays
            d += timedelta(days=7)
        return sorted(set(out))

    def _day(self, day: date) -> pd.DataFrame:
        if day in self._spot_cache:
            return self._spot_cache[day]
        rng = np.random.default_rng(self.seed * 100_000 + day.toordinal())
        # day open drifts deterministically from the ordinal so days chain smoothly
        base = self.start_level * (1 + 0.04 * math.sin(day.toordinal() / 37.0) + 0.02 * math.sin(day.toordinal() / 9.0))
        n = 375
        regime = rng.choice([-1, 1], size=6) * rng.uniform(0.00001, 0.00006, size=6)
        drift = np.repeat(regime, math.ceil(n / 6))[:n]
        rets = drift + rng.normal(0, 0.00045, size=n)
        close = base * np.exp(np.cumsum(rets))
        opn = np.concatenate([[base], close[:-1]])
        wig = np.abs(rng.normal(0, 0.00025, size=n)) * close
        high = np.maximum(opn, close) + wig
        low = np.minimum(opn, close) - np.abs(rng.normal(0, 0.00025, size=n)) * close
        idx = pd.date_range(datetime.combine(day, time(9, 15)), periods=n, freq="1min", name="ts")
        df = pd.DataFrame({"open": opn, "high": high, "low": low, "close": close,
                           "volume": rng.integers(1000, 5000, size=n).astype(float), "oi": np.nan}, index=idx).round(2)
        self._spot_cache[day] = df
        return df

    def spot(self, d_from: date, d_to: date) -> pd.DataFrame:
        frames = [self._day(d) for d in business_days(d_from, d_to)]
        return pd.concat(frames) if frames else pd.DataFrame()

    def option(self, contract: Contract, day: date) -> pd.DataFrame:
        s = self._day(day)
        exp_dt = pd.Timestamp(datetime.combine(contract.expiry, time(15, 30)))
        t = ((exp_dt - s.index) / pd.Timedelta(days=365)).to_numpy(dtype=float)
        vol = 0.12 + 0.03 * math.sin(day.toordinal() / 11.0)
        px = {k: bs_price(s[k].to_numpy(), contract.strike, t, vol, contract.opt_type) for k in ("open", "high", "low", "close")}
        hi_src, lo_src = ("high", "low") if contract.opt_type == "CE" else ("low", "high")
        df = pd.DataFrame({
            "open": px["open"], "close": px["close"],
            "high": np.maximum.reduce([px[hi_src], px["open"], px["close"]]),
            "low": np.minimum.reduce([px[lo_src], px["open"], px["close"]]),
            "volume": 10_000.0, "oi": np.nan}, index=s.index)
        for c in ("open", "high", "low", "close"):
            df[c] = np.maximum(TICK, np.round(df[c] / TICK) * TICK).round(2)
        return df


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
class DataLoader:
    def __init__(self, db: Database, source, progress: dict | None = None):
        self.db = db
        self.source = source
        self.progress = progress if progress is not None else {}

    def _p(self, **kw):
        self.progress.update(kw)

    def needed_contracts(self, spot: pd.DataFrame, expiries: list[date], d_from: date, d_to: date) -> dict:
        need: dict[date, set] = {}
        for tf in TIMEFRAMES:
            crosses = detect_crosses(add_emas(resample(spot, tf)))
            for c in crosses.itertuples(index=False):
                if not (d_from <= c.day <= d_to):
                    continue
                st = pd.Timestamp(c.signal_time).time()
                if not (WIDEST_START <= st <= WIDEST_CUTOFF):
                    continue
                exp = nearest_expiry(c.day, expiries)
                if exp is None:
                    continue
                for mode in MODES:
                    need.setdefault(c.day, set()).add(select_contract(mode, int(c.direction), c.spot_close, exp))
        return need

    def run(self, d_from: date, d_to: date, refresh_today: bool = True):
        today = date.today()
        self._p(state="running", step="Expiry calendar", done=0, total=0, message="", started=datetime.now().isoformat())
        expiries = self.source.expiries(d_from, d_to)
        self.db.add_expiries(expiries)

        # spot, fetched in 5-day chunks, only the days not fetched yet
        days = business_days(d_from - timedelta(days=14), d_to)  # 14 extra days warm up the EMAs
        have = self.db.fetched_days(SPOT_GROWW_SYMBOL)
        todo = [d for d in days if d not in have or (refresh_today and d == today)]
        self._p(step="NIFTY spot 1m candles", done=0, total=len(todo))
        for i in range(0, len(todo), 5):
            chunk = todo[i:i + 5]
            df = self.source.spot(chunk[0], chunk[-1])
            self.db.upsert_candles(SPOT_GROWW_SYMBOL, df)
            for d in chunk:
                n = int((df.index.date == d).sum()) if not df.empty else 0
                if d != today:
                    self.db.log_fetch(SPOT_GROWW_SYMBOL, d, n)
            self._p(done=min(i + 5, len(todo)))

        # options needed by any mode / timeframe / window
        warm = d_from - timedelta(days=14)
        spot = self.db.candles(SPOT_GROWW_SYMBOL, datetime.combine(warm, time(0)),
                               datetime.combine(d_to + timedelta(days=1), time(0)))
        need = self.needed_contracts(spot, self.db.expiries(), d_from, d_to)
        jobs = [(d, c) for d, cs in sorted(need.items()) for c in sorted(cs, key=lambda x: (x.strike, x.opt_type))]
        jobs = [(d, c) for d, c in jobs if (d == today and refresh_today) or not self.db.fetched(c.groww_symbol, d)]
        self._p(step="Option 1m candles", done=0, total=len(jobs))
        errors = 0
        for i, (d, c) in enumerate(jobs, 1):
            try:
                df = self.source.option(c, d)
                self.db.upsert_candles(c.groww_symbol, df)
                if d != today:
                    self.db.log_fetch(c.groww_symbol, d, len(df))
            except Exception as e:  # noqa: BLE001 - keep going, report count
                errors += 1
                log.warning("option fetch failed %s %s: %s", c.groww_symbol, d, e)
            self._p(done=i, message=f"{c.label} · {d}")
        self.db.set_meta("data_source", self.source.name)
        self.db.bump_data_version()
        self._p(state="done", step="Complete", message=f"{len(jobs)} option-days fetched, {errors} errors",
                finished=datetime.now().isoformat())


class LoaderJob:
    """Runs one DataLoader at a time in a background thread."""

    def __init__(self):
        self.progress: dict = {"state": "idle"}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, db: Database, source, d_from: date, d_to: date, wipe: bool = False) -> bool:
        with self._lock:
            if self.running:
                return False
            self.progress = {"state": "starting", "source": source.name}

            def work():
                try:
                    if wipe:
                        db.clear_market_data()
                    DataLoader(db, source, self.progress).run(d_from, d_to)
                except Exception as e:  # noqa: BLE001
                    log.error("loader failed: %s", traceback.format_exc())
                    self.progress.update(state="error", message=str(e))

            self._thread = threading.Thread(target=work, daemon=True, name="data-loader")
            self._thread.start()
            return True
