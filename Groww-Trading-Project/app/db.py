"""PostgreSQL storage: candles, expiries, backtest cache, live-engine journal."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from .config import SETTINGS, SPOT_GROWW_SYMBOL

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    ts TIMESTAMP NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION,
    oi DOUBLE PRECISION,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    symbol TEXT NOT NULL,
    day DATE NOT NULL,
    rows INTEGER NOT NULL,
    fetched_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS expiries (expiry DATE PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backtest_cache (
    key TEXT PRIMARY KEY,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    result JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS live_sessions (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP NOT NULL DEFAULT now(),
    stopped_at TIMESTAMP,
    trading_day DATE NOT NULL,
    mode TEXT NOT NULL,             -- PAPER / LIVE
    config JSONB NOT NULL,
    status TEXT NOT NULL            -- RUNNING / STOPPED / HALTED
);
CREATE TABLE IF NOT EXISTS live_positions (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES live_sessions(id),
    trading_day DATE NOT NULL,
    exec_mode TEXT NOT NULL,        -- PAPER / LIVE
    strategy_mode TEXT NOT NULL,    -- ITM5_BUY / ATM_BUY / ATM_SELL
    status TEXT NOT NULL,           -- OPEN / CLOSED
    data JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS live_orders (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES live_sessions(id),
    ts TIMESTAMP NOT NULL,
    ref TEXT NOT NULL,
    broker_order_id TEXT,
    trading_symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    status TEXT NOT NULL,
    fill_price DOUBLE PRECISION,
    raw JSONB
);
CREATE TABLE IF NOT EXISTS live_events (
    id SERIAL PRIMARY KEY,
    session_id INTEGER,
    ts TIMESTAMP NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data JSONB
);
"""


class Database:
    def __init__(self, url: str | None = None):
        self.url = url or SETTINGS.database_url
        self._init_lock = threading.Lock()
        self._ready = False

    @contextmanager
    def conn(self):
        with psycopg.connect(self.url, autocommit=False) as c:
            yield c

    def init(self):
        with self._init_lock:
            if self._ready:
                return
            with self.conn() as c:
                c.execute(SCHEMA)
                c.commit()
            self._ready = True

    def ping(self) -> bool:
        try:
            with self.conn() as c:
                c.execute("SELECT 1")
            return True
        except Exception:
            return False

    # -- meta -------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=%s", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str):
        with self.conn() as c:
            c.execute("INSERT INTO meta(key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                      (key, value))
            c.commit()

    def data_version(self) -> str:
        return self.get_meta("data_version", "0")

    def bump_data_version(self):
        self.set_meta("data_version", datetime.now().strftime("%Y%m%d%H%M%S%f"))

    # -- candles -----------------------------------------------------------------
    def upsert_candles(self, symbol: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = [(symbol, ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close),
                 None if pd.isna(getattr(r, "volume", None)) else float(r.volume),
                 None if pd.isna(getattr(r, "oi", None)) else float(r.oi))
                for ts, r in zip(df.index, df.itertuples(index=False))]
        with self.conn() as c:
            with c.cursor() as cur:
                cur.executemany(
                    """INSERT INTO candles(symbol, ts, open, high, low, close, volume, oi)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (symbol, ts) DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high,
                       low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume, oi=EXCLUDED.oi""",
                    rows)
            c.commit()
        return len(rows)

    def candles(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        with self.conn() as c:
            rows = c.execute(
                "SELECT ts, open, high, low, close, volume, oi FROM candles WHERE symbol=%s AND ts>=%s AND ts<%s ORDER BY ts",
                (symbol, start, end)).fetchall()
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "oi"])
        df["ts"] = pd.to_datetime(df["ts"])
        return df.set_index("ts")

    def log_fetch(self, symbol: str, day: date, rows: int):
        with self.conn() as c:
            c.execute("""INSERT INTO fetch_log(symbol, day, rows) VALUES (%s,%s,%s)
                         ON CONFLICT (symbol, day) DO UPDATE SET rows=EXCLUDED.rows, fetched_at=now()""",
                      (symbol, day, rows))
            c.commit()

    def fetched(self, symbol: str, day: date) -> bool:
        with self.conn() as c:
            return c.execute("SELECT 1 FROM fetch_log WHERE symbol=%s AND day=%s", (symbol, day)).fetchone() is not None

    def fetched_days(self, symbol: str) -> set[date]:
        with self.conn() as c:
            return {r[0] for r in c.execute("SELECT day FROM fetch_log WHERE symbol=%s", (symbol,)).fetchall()}

    def coverage(self) -> dict:
        with self.conn() as c:
            spot = c.execute("SELECT min(ts), max(ts), count(*), count(DISTINCT ts::date) FROM candles WHERE symbol=%s",
                             (SPOT_GROWW_SYMBOL,)).fetchone()
            opts = c.execute("SELECT count(DISTINCT symbol), count(*) FROM candles WHERE symbol<>%s",
                             (SPOT_GROWW_SYMBOL,)).fetchone()
            exp = c.execute("SELECT min(expiry), max(expiry), count(*) FROM expiries").fetchone()
        return {
            "spot_from": spot[0].date().isoformat() if spot[0] else None,
            "spot_to": spot[1].date().isoformat() if spot[1] else None,
            "spot_rows": spot[2], "spot_days": spot[3],
            "option_contracts": opts[0], "option_rows": opts[1],
            "expiry_from": exp[0].isoformat() if exp[0] else None,
            "expiry_to": exp[1].isoformat() if exp[1] else None,
            "expiries": exp[2],
            "source": self.get_meta("data_source", "none"),
            "version": self.data_version(),
        }

    def clear_market_data(self):
        with self.conn() as c:
            c.execute("TRUNCATE candles, fetch_log, expiries, backtest_cache")
            c.commit()

    # -- expiries ------------------------------------------------------------------
    def add_expiries(self, days: list[date]):
        if not days:
            return
        with self.conn() as c:
            with c.cursor() as cur:
                cur.executemany("INSERT INTO expiries(expiry) VALUES (%s) ON CONFLICT DO NOTHING", [(d,) for d in days])
            c.commit()

    def expiries(self) -> list[date]:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT expiry FROM expiries ORDER BY expiry").fetchall()]

    # -- backtest cache ------------------------------------------------------------
    def cache_get(self, key: str):
        with self.conn() as c:
            row = c.execute("SELECT result FROM backtest_cache WHERE key=%s", (key,)).fetchone()
        return row[0] if row else None

    def cache_put(self, key: str, result: dict):
        with self.conn() as c:
            c.execute("""INSERT INTO backtest_cache(key, result) VALUES (%s,%s)
                         ON CONFLICT (key) DO UPDATE SET result=EXCLUDED.result, created_at=now()""",
                      (key, Jsonb(json.loads(json.dumps(result, default=str)))))
            c.execute("DELETE FROM backtest_cache WHERE created_at < now() - interval '30 days'")
            c.commit()

    # -- live journal ----------------------------------------------------------------
    def create_session(self, trading_day: date, mode: str, config: dict) -> int:
        with self.conn() as c:
            sid = c.execute("""INSERT INTO live_sessions(trading_day, mode, config, status)
                               VALUES (%s,%s,%s,'RUNNING') RETURNING id""",
                            (trading_day, mode, Jsonb(config))).fetchone()[0]
            c.commit()
        return sid

    def end_session(self, sid: int, status: str):
        with self.conn() as c:
            c.execute("UPDATE live_sessions SET status=%s, stopped_at=now() WHERE id=%s", (status, sid))
            c.commit()

    def mark_orphan_sessions(self):
        """Any session still RUNNING at process start was interrupted (crash / restart)."""
        with self.conn() as c:
            c.execute("UPDATE live_sessions SET status='INTERRUPTED', stopped_at=now() WHERE status='RUNNING'")
            c.commit()

    def sessions(self, limit: int = 20) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("""SELECT id, started_at, stopped_at, trading_day, mode, config, status
                                FROM live_sessions ORDER BY id DESC LIMIT %s""", (limit,)).fetchall()
        return [{"id": r[0], "started_at": r[1].isoformat(), "stopped_at": r[2].isoformat() if r[2] else None,
                 "trading_day": r[3].isoformat(), "mode": r[4], "config": r[5], "status": r[6]} for r in rows]

    def save_position(self, pos_id: int | None, session_id: int, trading_day: date, exec_mode: str,
                      strategy_mode: str, status: str, data: dict) -> int:
        payload = Jsonb(json.loads(json.dumps(data, default=str)))
        with self.conn() as c:
            if pos_id is None:
                pos_id = c.execute("""INSERT INTO live_positions(session_id, trading_day, exec_mode, strategy_mode,
                                      status, data) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                                   (session_id, trading_day, exec_mode, strategy_mode, status, payload)).fetchone()[0]
            else:
                c.execute("UPDATE live_positions SET status=%s, data=%s WHERE id=%s", (status, payload, pos_id))
            c.commit()
        return pos_id

    def open_positions(self, trading_day: date, exec_mode: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("""SELECT id, session_id, strategy_mode, data FROM live_positions
                                WHERE trading_day=%s AND exec_mode=%s AND status='OPEN' ORDER BY id""",
                             (trading_day, exec_mode)).fetchall()
        return [{"id": r[0], "session_id": r[1], "strategy_mode": r[2], **r[3]} for r in rows]

    def positions(self, trading_day: date | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT id, session_id, trading_day, exec_mode, strategy_mode, status, data FROM live_positions"
        args: tuple = ()
        if trading_day:
            q += " WHERE trading_day=%s"
            args = (trading_day,)
        q += " ORDER BY id DESC LIMIT %s"
        with self.conn() as c:
            rows = c.execute(q, args + (limit,)).fetchall()
        return [{"id": r[0], "session_id": r[1], "trading_day": r[2].isoformat(), "exec_mode": r[3],
                 "strategy_mode": r[4], "status": r[5], **r[6]} for r in rows]

    def log_order(self, session_id, ts, ref, broker_id, symbol, side, qty, status, fill_price, raw):
        with self.conn() as c:
            c.execute("""INSERT INTO live_orders(session_id, ts, ref, broker_order_id, trading_symbol, side, qty,
                         status, fill_price, raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (session_id, ts, ref, broker_id, symbol, side, qty, status, fill_price,
                       Jsonb(json.loads(json.dumps(raw or {}, default=str)))))
            c.commit()

    def orders(self, session_id: int | None = None, limit: int = 100) -> list[dict]:
        q = "SELECT id, session_id, ts, ref, broker_order_id, trading_symbol, side, qty, status, fill_price FROM live_orders"
        args: tuple = ()
        if session_id:
            q += " WHERE session_id=%s"
            args = (session_id,)
        q += " ORDER BY id DESC LIMIT %s"
        with self.conn() as c:
            rows = c.execute(q, args + (limit,)).fetchall()
        keys = ["id", "session_id", "ts", "ref", "broker_order_id", "trading_symbol", "side", "qty", "status", "fill_price"]
        return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in zip(keys, r)} for r in rows]

    def log_event(self, session_id, ts, level, message, data=None):
        with self.conn() as c:
            c.execute("INSERT INTO live_events(session_id, ts, level, message, data) VALUES (%s,%s,%s,%s,%s)",
                      (session_id, ts, level, message, Jsonb(json.loads(json.dumps(data or {}, default=str)))))
            c.commit()

    def events(self, session_id: int | None = None, limit: int = 200, after_id: int = 0) -> list[dict]:
        q = "SELECT id, session_id, ts, level, message, data FROM live_events WHERE id>%s"
        args: tuple = (after_id,)
        if session_id:
            q += " AND session_id=%s"
            args += (session_id,)
        q += " ORDER BY id DESC LIMIT %s"
        with self.conn() as c:
            rows = c.execute(q, args + (limit,)).fetchall()
        return [{"id": r[0], "session_id": r[1], "ts": r[2].isoformat(), "level": r[3], "message": r[4],
                 "data": r[5]} for r in rows]


class PgStore:
    """Read-side adapter the backtester uses (spot_1m / expiries / option_1m)."""

    def __init__(self, db: Database):
        self.db = db

    def spot_1m(self, d_from: date, d_to: date) -> pd.DataFrame:
        return self.db.candles(SPOT_GROWW_SYMBOL, datetime.combine(d_from, datetime.min.time()),
                               datetime.combine(d_to + timedelta(days=1), datetime.min.time()))

    def expiries(self) -> list[date]:
        return self.db.expiries()

    def option_1m(self, groww_symbol: str, day: date) -> pd.DataFrame:
        return self.db.candles(groww_symbol, datetime.combine(day, datetime.min.time()),
                               datetime.combine(day + timedelta(days=1), datetime.min.time()))
