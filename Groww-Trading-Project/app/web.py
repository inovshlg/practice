"""Flask application: pages + JSON API."""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import date, datetime, time, timedelta

import pandas as pd
from flask import Flask, jsonify, render_template, request

from .config import (DEFAULT_RISK, ENTRY_CUTOFF_CHOICES, ENTRY_START_CHOICES, LOT_SIZE, MODE_LABELS, MODES,
                     SETTINGS, SPOT_GROWW_SYMBOL, TIMEFRAMES, RiskLimits)
from .data_loader import DemoSource, GrowwSource, LoaderJob
from .db import Database, PgStore
from .groww_client import GrowwAuthError, get_client
from .live.engine import GrowwBroker, GrowwMarketData, LiveConfig, PaperBroker, TradingEngine, replay_day
from .strategy.backtest import BacktestParams, run_backtest
from .strategy.indicators import add_emas, resample
from .timeutil import now_naive

log = logging.getLogger(__name__)


class LRU(OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self.size = size
        self.lock = threading.Lock()

    def get_or(self, key, fn):
        with self.lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        val = fn()
        with self.lock:
            self[key] = val
            while len(self) > self.size:
                self.popitem(last=False)
        return val


def _parse_date(s: str | None, default: date) -> date:
    return date.fromisoformat(s) if s else default


def _parse_time(s: str | None, default: time) -> time:
    return datetime.strptime(s, "%H:%M").time() if s else default


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def create_app(db: Database | None = None, groww_client=None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SETTINGS.secret_key
    app.json.sort_keys = False

    db = db or Database()
    try:
        db.init()
        db.mark_orphan_sessions()
    except Exception as e:  # noqa: BLE001
        log.error("Database unavailable: %s", e)
    store = PgStore(db)
    loader = LoaderJob()
    bt_cache = LRU(24)
    chart_cache = LRU(256)
    engine_box: dict = {"engine": None}
    client_getter = (lambda: groww_client) if groww_client is not None else get_client

    app.extensions["tp"] = {"db": db, "loader": loader, "engine_box": engine_box, "bt_cache": bt_cache}

    @app.context_processor
    def inject():
        return {"MODES": MODES, "MODE_LABELS": MODE_LABELS, "TIMEFRAMES": TIMEFRAMES,
                "STARTS": [t.strftime("%H:%M") for t in ENTRY_START_CHOICES],
                "CUTOFFS": [t.strftime("%H:%M") for t in ENTRY_CUTOFF_CHOICES], "LOT_SIZE": LOT_SIZE,
                "RISK": DEFAULT_RISK}

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def page_account():
        return render_template("account.html", page="account")

    @app.get("/backtest")
    def page_backtest():
        return render_template("backtest.html", page="backtest")

    @app.get("/live")
    def page_live():
        return render_template("live.html", page="live")

    @app.get("/data")
    def page_data():
        return render_template("data.html", page="data")

    # ------------------------------------------------------------------ health / account
    @app.get("/api/health")
    def api_health():
        return jsonify({"db": db.ping(), "keys_file": SETTINGS.keys_file.exists(),
                        "server_time_ist": now_naive().isoformat(timespec="seconds")})

    @app.get("/api/account")
    def api_account():
        try:
            c = client_getter()
            out = {"profile": c.profile(), "margins": c.margins()}
            try:
                out["positions"] = c.positions()
            except Exception as e:  # noqa: BLE001
                out["positions_error"] = str(e)
            return jsonify(out)
        except GrowwAuthError as e:
            return _err(str(e), 401)
        except Exception as e:  # noqa: BLE001
            return _err(f"Groww API error: {e}", 502)

    # ------------------------------------------------------------------ data
    @app.get("/api/data/coverage")
    def api_coverage():
        try:
            return jsonify(db.coverage())
        except Exception as e:  # noqa: BLE001
            return _err(f"Database error: {e}", 500)

    @app.post("/api/data/download")
    def api_download():
        body = request.get_json(force=True, silent=True) or {}
        try:
            d_to = _parse_date(body.get("to"), date.today())
            d_from = _parse_date(body.get("from"), d_to - timedelta(days=30))
        except ValueError:
            return _err("dates must be YYYY-MM-DD")
        if d_from > d_to:
            return _err("'from' is after 'to'")
        if (d_to - d_from).days > 400:
            return _err("range too large (max ~13 months per run)")
        src_name = body.get("source", "groww")
        current = db.get_meta("data_source", "none")
        if src_name == "demo":
            source = DemoSource()
        else:
            try:
                source = GrowwSource(client_getter())
                _ = client_getter().api  # fail fast on auth problems
            except GrowwAuthError as e:
                return _err(str(e), 401)
            except Exception as e:  # noqa: BLE001
                return _err(f"Groww connection failed: {e}", 502)
        wipe = bool(body.get("wipe")) or (current not in ("none", source.name))
        if not loader.start(db, source, d_from, d_to, wipe=wipe):
            return _err("a download is already running", 409)
        bt_cache.clear()
        chart_cache.clear()
        return jsonify({"started": True, "wiped": wipe})

    @app.get("/api/data/progress")
    def api_progress():
        return jsonify({**loader.progress, "running": loader.running})

    # ------------------------------------------------------------------ backtest
    def _params_from(body) -> BacktestParams:
        today = date.today()
        return BacktestParams(
            start_date=_parse_date(body.get("from"), today - timedelta(days=60)),
            end_date=_parse_date(body.get("to"), today),
            timeframe=int(body.get("timeframe", 15)),
            entry_start=_parse_time(body.get("entry_start"), time(9, 30)),
            entry_cutoff=_parse_time(body.get("entry_cutoff"), time(14, 30)),
            lots=int(body.get("lots", 1)),
        )

    @app.post("/api/backtest")
    def api_backtest():
        body = request.get_json(force=True, silent=True) or {}
        try:
            p = _params_from(body)
        except (ValueError, TypeError) as e:
            return _err(str(e))
        version = db.data_version()
        key = p.cache_key(version)

        def compute():
            cached = db.cache_get(key)
            if cached:
                cached["cache"] = "db"
                return cached
            res = run_backtest(store, p)
            res["key"] = key
            res["data_source"] = db.get_meta("data_source", "none")
            db.cache_put(key, res)
            res["cache"] = "computed"
            return res

        try:
            hit = key in bt_cache
            res = bt_cache.get_or(key, compute)
            out = dict(res)
            if hit:
                out["cache"] = "memory"
            return jsonify(out)
        except Exception as e:  # noqa: BLE001
            log.exception("backtest failed")
            return _err(f"Backtest failed: {e}", 500)

    @app.get("/api/chart")
    def api_chart():
        """Candles + EMAs for one trade's day; option premium candles for the contract."""
        try:
            day = date.fromisoformat(request.args["day"])
            tf = int(request.args.get("tf", 15))
            symbol = request.args.get("symbol")
        except (KeyError, ValueError):
            return _err("day (YYYY-MM-DD) and tf are required")
        if tf not in TIMEFRAMES:
            return _err("bad timeframe")
        version = db.data_version()

        def build():
            spot = store.spot_1m(day - timedelta(days=14), day)
            bars = add_emas(resample(spot, tf))
            day_bars = bars[bars.index.date == day]

            def ts(ix):
                # lightweight-charts treats epoch seconds as UTC: send IST wall-clock as if UTC
                return int(pd.Timestamp(ix).tz_localize("UTC").timestamp())

            out = {"spot": [{"time": ts(ix), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                            for ix, r in day_bars.iterrows()],
                   "ema9": [{"time": ts(ix), "value": round(v, 2)} for ix, v in day_bars["ema9"].items()
                            if pd.notna(v)],
                   "ema21": [{"time": ts(ix), "value": round(v, 2)} for ix, v in day_bars["ema21"].items()
                             if pd.notna(v)],
                   "option": []}
            if symbol:
                opt = store.option_1m(symbol, day)
                out["option"] = [{"time": ts(ix), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                                 for ix, r in opt.iterrows()]
            return out

        try:
            return jsonify(chart_cache.get_or((version, day, tf, symbol), build))
        except Exception as e:  # noqa: BLE001
            log.exception("chart failed")
            return _err(f"Chart failed: {e}", 500)

    # ------------------------------------------------------------------ live engine
    def _engine() -> TradingEngine | None:
        return engine_box["engine"]

    @app.get("/api/live/state")
    def api_live_state():
        eng = _engine()
        if eng is None:
            return jsonify({"status": "IDLE"})
        return jsonify(eng.state())

    @app.post("/api/live/start")
    def api_live_start():
        body = request.get_json(force=True, silent=True) or {}
        eng = _engine()
        if eng is not None and eng.status == "RUNNING":
            return _err("engine already running - stop it first", 409)
        if eng is not None and eng.position is not None:
            return _err("previous engine still holds a position - stop it with 'flatten' first", 409)
        exec_mode = body.get("exec_mode", "PAPER")
        if exec_mode == "LIVE" and body.get("confirm") != "LIVE":
            return _err("type LIVE in the confirmation box to trade real money")
        try:
            risk = RiskLimits(
                max_trades_per_day=int(body.get("max_trades", DEFAULT_RISK.max_trades_per_day)),
                max_loss_per_day=float(body.get("max_loss", DEFAULT_RISK.max_loss_per_day)),
                max_lots=DEFAULT_RISK.max_lots,
            )
            cfg = LiveConfig(exec_mode=exec_mode, strategy_mode=body.get("strategy_mode", "ATM_BUY"),
                             timeframe=int(body.get("timeframe", 15)),
                             entry_start=body.get("entry_start", "09:30"),
                             entry_cutoff=body.get("entry_cutoff", "14:30"),
                             lots=int(body.get("lots", 1)), risk=risk)
            cfg.validate()
        except (ValueError, TypeError) as e:
            return _err(str(e))
        now = now_naive()
        if now.weekday() >= 5 and not body.get("allow_weekend"):
            return _err("market is closed today (weekend)")
        if now.time() >= time(15, 0):
            return _err("after 15:00 - the strategy does not open new sessions")
        try:
            client = client_getter()
            _ = client.api
        except GrowwAuthError as e:
            return _err(str(e), 401)
        except Exception as e:  # noqa: BLE001
            return _err(f"Groww connection failed: {e}", 502)
        market = GrowwMarketData(client, db)
        broker = PaperBroker(market) if exec_mode == "PAPER" else GrowwBroker(client, DEFAULT_RISK.order_fill_timeout_s)
        eng = TradingEngine(cfg, market, broker, db, clock=now_naive)
        engine_box["engine"] = eng
        eng.start_thread()
        return jsonify(eng.state())

    @app.post("/api/live/stop")
    def api_live_stop():
        body = request.get_json(force=True, silent=True) or {}
        eng = _engine()
        if eng is None:
            return _err("engine is not running", 409)
        eng.stop(flatten=bool(body.get("flatten", True)))
        return jsonify(eng.state())

    @app.get("/api/live/history")
    def api_live_history():
        try:
            return jsonify({"sessions": db.sessions(20), "positions": db.positions(limit=100),
                            "orders": db.orders(limit=100)})
        except Exception as e:  # noqa: BLE001
            return _err(f"Database error: {e}", 500)

    @app.post("/api/live/replay")
    def api_live_replay():
        body = request.get_json(force=True, silent=True) or {}
        try:
            day = date.fromisoformat(body["day"])
            cfg = LiveConfig(strategy_mode=body.get("strategy_mode", "ATM_BUY"),
                             timeframe=int(body.get("timeframe", 15)),
                             entry_start=body.get("entry_start", "09:30"),
                             entry_cutoff=body.get("entry_cutoff", "14:30"), lots=int(body.get("lots", 1)),
                             risk=RiskLimits(max_trades_per_day=int(body.get("max_trades", 99)),
                                             max_loss_per_day=float(body.get("max_loss", 1e12))))
            cfg.validate()
        except (KeyError, ValueError, TypeError) as e:
            return _err(f"bad request: {e}")
        if store.spot_1m(day, day).empty:
            return _err(f"no stored spot data for {day} - download it on the Data page first", 404)
        return jsonify(replay_day(store, day, cfg))

    return app
