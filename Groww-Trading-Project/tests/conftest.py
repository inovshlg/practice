import os
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("TEST_DATABASE_URL", "postgresql://groww:groww@localhost:5432/groww_trading_test")


def minute_index(day: date, start=time(9, 15), end=time(15, 30)):
    return pd.date_range(datetime.combine(day, start), datetime.combine(day, end) - timedelta(minutes=1),
                         freq="1min", name="ts")


def flat_ohlc(index, values):
    v = np.asarray(values, dtype=float)
    return pd.DataFrame({"open": v, "high": v, "low": v, "close": v, "volume": 1.0, "oi": np.nan}, index=index)


class MemStore:
    """In-memory store with the same interface as PgStore."""

    def __init__(self, spot: pd.DataFrame, options: dict | None = None, expiries=None):
        self.spot = spot.sort_index()
        self.options = options or {}
        self._exp = expiries or []
        self.default_option = None  # callable(symbol, day) -> df

    def spot_1m(self, d_from, d_to):
        s = self.spot
        return s[(s.index.date >= d_from) & (s.index.date <= d_to)]

    def expiries(self):
        return list(self._exp)

    def option_1m(self, symbol, day):
        if (symbol, day) in self.options:
            return self.options[(symbol, day)]
        if self.default_option:
            return self.default_option(symbol, day)
        return pd.DataFrame(columns=["open", "high", "low", "close"], index=pd.DatetimeIndex([]))


def _fresh_db():
    from app.db import Database
    db = Database(os.environ["TEST_DATABASE_URL"])
    try:
        db.init()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"postgres not available: {e}")
    with db.conn() as c:
        c.execute("TRUNCATE candles, fetch_log, expiries, meta, backtest_cache, live_events, live_orders, "
                  "live_positions, live_sessions RESTART IDENTITY CASCADE")
        c.commit()
    return db


@pytest.fixture
def test_db():
    return _fresh_db()


@pytest.fixture(scope="module")
def module_db():
    return _fresh_db()
