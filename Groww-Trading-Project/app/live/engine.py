"""Live / paper trading engine for the 9/21 EMA crossover.

The engine re-uses the backtester's building blocks verbatim:
  indicators.signal_frame  -> closed bars, EMAs, crossovers
  rules.actions_for_cross  -> exit / reversal / entry decisions
  contracts.select_contract-> strike / CE-PE / expiry
  costs.round_trip         -> friction + net PnL

Execution modes
  PAPER : orders are simulated at the option LTP plus the backtest slippage model.
  LIVE  : MARKET / MIS orders are sent to Groww; the engine waits for the fill and
          books the broker's average fill price.

Safety
  * only crosses that close AFTER the engine started are traded (no stale signals)
  * one open position at a time, max trades/day, max realised loss/day, max lots
  * stale-data guard: no new entries if the latest 1m spot candle is too old
  * mandatory square-off at 15:00; Stop can flatten the book
  * if a LIVE exit order cannot be confirmed the engine HALTS and asks for manual action
  * every decision, order and fill is journaled to Postgres; on restart the open
    position for today is re-adopted
"""
from __future__ import annotations

import logging
import threading
import time as _time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from ..config import DEFAULT_FRICTION, DEFAULT_RISK, LOT_SIZE, SESSION_OPEN, SQUARE_OFF, RiskLimits
from ..strategy.contracts import Contract, nearest_expiry, option_side, select_contract
from ..strategy.costs import apply_slippage, round_trip
from ..strategy.indicators import signal_frame
from ..strategy.rules import actions_for_cross

log = logging.getLogger(__name__)

FILLED = {"EXECUTED", "COMPLETED", "COMPLETE", "FILLED"}
DEAD = {"REJECTED", "FAILED", "CANCELLED", "CANCELED"}


@dataclass
class LiveConfig:
    exec_mode: str = "PAPER"            # PAPER / LIVE
    strategy_mode: str = "ATM_BUY"      # ITM5_BUY / ATM_BUY / ATM_SELL
    timeframe: int = 15
    entry_start: str = "09:30"
    entry_cutoff: str = "14:30"
    lots: int = 1
    poll_seconds: float = 5.0
    risk: RiskLimits = field(default_factory=lambda: DEFAULT_RISK)

    def validate(self):
        if self.exec_mode not in ("PAPER", "LIVE"):
            raise ValueError("exec_mode must be PAPER or LIVE")
        if self.strategy_mode not in ("ITM5_BUY", "ATM_BUY", "ATM_SELL"):
            raise ValueError("unknown strategy mode")
        if self.timeframe not in (5, 15, 30):
            raise ValueError("timeframe must be 5, 15 or 30")
        if not (1 <= self.lots <= self.risk.max_lots):
            raise ValueError(f"lots must be between 1 and {self.risk.max_lots}")
        s, c = self.start_t, self.cutoff_t
        if not (time(9, 15) <= s < c <= SQUARE_OFF):
            raise ValueError("entry window must satisfy 09:15 <= start < cutoff <= 15:00")

    @property
    def start_t(self) -> time:
        return datetime.strptime(self.entry_start, "%H:%M").time()

    @property
    def cutoff_t(self) -> time:
        return datetime.strptime(self.entry_cutoff, "%H:%M").time()

    @property
    def qty(self) -> int:
        return self.lots * LOT_SIZE

    def to_dict(self):
        d = asdict(self)
        return d


@dataclass
class Fill:
    ok: bool
    price: float | None
    status: str
    broker_id: str | None = None
    raw: dict | None = None


# ---------------------------------------------------------------------------
# Market data + brokers
# ---------------------------------------------------------------------------
class GrowwMarketData:
    """Spot candles: history from Postgres (warm-up) + today's candles from Groww."""

    def __init__(self, client, db):
        self.client = client
        self.db = db
        self._hist_day: date | None = None
        self._hist: pd.DataFrame | None = None
        self._expiries: list[date] = []
        self._tsym: dict[str, str] = {}

    def _history(self, day: date) -> pd.DataFrame:
        if self._hist_day != day:
            from ..config import SPOT_GROWW_SYMBOL
            start = datetime.combine(day - timedelta(days=10), time(0))
            end = datetime.combine(day, time(0))
            hist = self.db.candles(SPOT_GROWW_SYMBOL, start, end)
            if hist.empty or (day - hist.index[-1].date()).days > 5:
                hist = self.client.candles(SPOT_GROWW_SYMBOL, start, end - timedelta(minutes=1), "CASH")
                self.db.upsert_candles(SPOT_GROWW_SYMBOL, hist)
            self._hist, self._hist_day = hist, day
        return self._hist

    def spot_1m(self, now: datetime) -> pd.DataFrame:
        from ..config import SPOT_GROWW_SYMBOL
        today = self.client.candles(SPOT_GROWW_SYMBOL, datetime.combine(now.date(), SESSION_OPEN), now, "CASH")
        # the candle for the current minute is still forming: keep only completed minutes
        today = today[today.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
        return pd.concat([self._history(now.date()), today])

    def expiries(self, day: date) -> list[date]:
        if not self._expiries or max(self._expiries) < day:
            exp = set(self.client.expiries(day.year))
            if day.month == 12:
                exp.update(self.client.expiries(day.year + 1))
            self._expiries = sorted(exp)
            self.db.add_expiries(self._expiries)
        return self._expiries

    def trading_symbol(self, c: Contract) -> str:
        if c.groww_symbol not in self._tsym:
            self._tsym[c.groww_symbol] = self.client.option_trading_symbol(c.expiry, c.strike, c.opt_type,
                                                                           c.groww_symbol)
        return self._tsym[c.groww_symbol]

    def option_ltp(self, c: Contract, now: datetime) -> float | None:
        ts = self.trading_symbol(c)
        return self.client.ltp([ts], "FNO").get(ts)


class PaperBroker:
    name = "PAPER"

    def __init__(self, market):
        self.market = market

    def execute(self, contract: Contract, tsym: str, side: str, qty: int, ref: str, now: datetime) -> Fill:
        ltp = self.market.option_ltp(contract, now)
        if ltp is None:
            return Fill(False, None, "NO_PRICE")
        # paper books the RAW price; slippage is applied once, in round_trip(), exactly like the backtest
        return Fill(True, float(ltp), "PAPER_FILLED", broker_id=f"PAPER-{ref}",
                    raw={"ltp": ltp, "sim_fill": apply_slippage(ltp, side)})


class GrowwBroker:
    name = "LIVE"

    def __init__(self, client, timeout_s: int = 30, sleep=_time.sleep):
        self.client = client
        self.timeout_s = timeout_s
        self.sleep = sleep

    def execute(self, contract: Contract, tsym: str, side: str, qty: int, ref: str, now: datetime) -> Fill:
        try:
            res = self.client.place_market_order(tsym, qty, side, ref)
        except Exception as e:  # noqa: BLE001
            return Fill(False, None, f"PLACE_FAILED: {e}")
        oid = res.get("groww_order_id")
        status = str(res.get("order_status", "")).upper()
        waited = 0.0
        detail = res
        while status not in FILLED and status not in DEAD and waited < self.timeout_s:
            self.sleep(1.0)
            waited += 1.0
            try:
                detail = self.client.order_detail(oid)
                status = str(detail.get("order_status", "")).upper()
            except Exception as e:  # noqa: BLE001
                log.warning("order status poll failed: %s", e)
        if status in FILLED:
            px = detail.get("average_fill_price") or detail.get("price")
            return Fill(True, float(px) if px else None, status, oid, detail)
        if status in DEAD:
            return Fill(False, None, status, oid, detail)
        return Fill(False, None, "UNCONFIRMED", oid, detail)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class TradingEngine:
    def __init__(self, cfg: LiveConfig, market, broker, db, clock=None, journal: bool = True):
        cfg.validate()
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.db = db
        self.clock = clock or (lambda: datetime.now())
        self.journal = journal and db is not None
        self.session_id: int | None = None
        self.status = "IDLE"
        self.halt_reason: str | None = None
        self.started_at: datetime | None = None
        self.trading_day: date | None = None
        self.position: dict | None = None
        self.closed: list[dict] = []
        self.processed: set = set()
        self.entry_count = 0
        self.last_tick: datetime | None = None
        self.last_error: str | None = None
        self.snapshot: dict = {}
        self.events: list[dict] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # -- journal -----------------------------------------------------------------
    def _event(self, level: str, msg: str, **data):
        now = self.clock()
        ev = {"ts": now.isoformat(timespec="seconds"), "level": level, "message": msg, "data": data}
        self.events.append(ev)
        self.events = self.events[-300:]
        getattr(log, "warning" if level in ("WARN", "ERROR", "HALT") else "info")("[%s] %s", level, msg)
        if self.journal:
            try:
                self.db.log_event(self.session_id, now, level, msg, data)
            except Exception:  # noqa: BLE001
                log.exception("journal failed")

    def _persist_position(self, pos: dict, status: str):
        if not self.journal:
            return
        data = {k: v for k, v in pos.items() if k not in ("contract", "db_id")}
        pos["db_id"] = self.db.save_position(pos.get("db_id"), self.session_id, self.trading_day,
                                             self.cfg.exec_mode, self.cfg.strategy_mode, status, data)

    # -- lifecycle ------------------------------------------------------------------
    def begin(self):
        now = self.clock()
        self.started_at = now
        self.trading_day = now.date()
        self.status = "RUNNING"
        if self.journal:
            self.session_id = self.db.create_session(self.trading_day, self.cfg.exec_mode, self.cfg.to_dict())
            self._adopt_open_position()
        self._event("INFO", f"{self.cfg.exec_mode} session started · {self.cfg.strategy_mode} · "
                            f"{self.cfg.timeframe}m · window {self.cfg.entry_start}-{self.cfg.entry_cutoff} · "
                            f"{self.cfg.lots} lot(s)")

    def _adopt_open_position(self):
        for p in self.db.open_positions(self.trading_day, self.cfg.exec_mode):
            if p.get("strategy_mode") != self.cfg.strategy_mode and p.get("mode") != self.cfg.strategy_mode:
                continue
            c = Contract(date.fromisoformat(p["expiry"]), int(p["strike"]), p["opt_type"])
            p["contract"] = c
            p["db_id"] = p.pop("id")
            self.position = p
            self.entry_count = p.get("entry_no", 1)
            self._event("WARN", f"Re-adopted open position from an earlier session: {c.label}. "
                                "Verify it in your Groww positions.", position_id=p["db_id"])
            break

    def start_thread(self):
        self.begin()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="trading-engine")
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                self._event("ERROR", f"tick failed: {e}")
            if self.status in ("STOPPED", "HALTED", "DONE") and self.position is None:
                break
            self._stop.wait(self.cfg.poll_seconds)
        if self.journal and self.session_id:
            self.db.end_session(self.session_id, self.status if self.status != "RUNNING" else "STOPPED")

    def stop(self, flatten: bool = True):
        with self._lock:
            if flatten and self.position is not None:
                self._exit(self.clock(), "MANUAL_STOP")
            self.status = "STOPPED"
            self._event("INFO", "Engine stopped by user" + (" (book flattened)" if flatten else ""))
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        if self.journal and self.session_id:
            self.db.end_session(self.session_id, "STOPPED")

    def halt(self, reason: str):
        self.status = "HALTED"
        self.halt_reason = reason
        self._event("HALT", reason)

    # -- one iteration ---------------------------------------------------------------
    def tick(self, now: datetime | None = None):
        with self._lock:
            now = now or self.clock()
            self.last_tick = now
            if now.date() != self.trading_day:
                self.status = "DONE"
                self._event("INFO", "Trading day changed - session finished")
                return
            if now.time() < SESSION_OPEN:
                self.snapshot["note"] = "Waiting for market open (09:15)"
                return
            sq = datetime.combine(self.trading_day, SQUARE_OFF)
            if now >= sq:
                if self.position is not None:
                    self._exit(now, "SQUARE_OFF_15:00")
                if self.status == "RUNNING":
                    self.status = "DONE"
                    self._event("INFO", "15:00 reached - day complete")
                return

            spot = self.market.spot_1m(now)
            bars, crosses = signal_frame(spot, self.cfg.timeframe, now=now)
            self._update_snapshot(now, spot, bars)
            if spot.empty:
                return
            stale = (now - spot.index[-1].to_pydatetime()).total_seconds() > self.cfg.risk.data_stale_after_s
            if stale:
                self.snapshot["note"] = "Spot data is stale - new entries paused"
            today = crosses[crosses["day"] == self.trading_day] if not crosses.empty else crosses
            for c in today.itertuples(index=False):
                sig_t = pd.Timestamp(c.signal_time).to_pydatetime()
                if sig_t in self.processed:
                    continue
                self.processed.add(sig_t)
                if sig_t < self.started_at - timedelta(seconds=90):
                    self._event("INFO", f"Ignoring cross at {sig_t:%H:%M} (closed before the engine started)")
                    continue
                self._on_cross(c, sig_t, now, stale)
            self._mark_to_market(now)

    def _on_cross(self, c, sig_t: datetime, now: datetime, stale: bool):
        in_window = self.cfg.start_t <= sig_t.time() <= self.cfg.cutoff_t
        dirn = "BULLISH" if c.direction > 0 else "BEARISH"
        self._event("SIGNAL", f"{dirn} 9/21 cross on the {self.cfg.timeframe}m bar closing {sig_t:%H:%M} "
                              f"(spot {c.spot_close:.2f}, {'inside' if in_window else 'outside'} entry window)",
                    direction=int(c.direction), spot=c.spot_close, ema9=c.ema9, ema21=c.ema21)
        for act, arg in actions_for_cross(self.position["direction"] if self.position else None,
                                          int(c.direction), in_window):
            if act == "EXIT":
                if not self._exit(now, arg):
                    return  # exit failed -> halted, never stack a new entry
            else:
                if self.status != "RUNNING":
                    self._event("INFO", f"Entry skipped: engine is {self.status}")
                elif stale:
                    self._event("WARN", "Entry skipped: spot data stale")
                else:
                    self._enter(c, sig_t, now)

    # -- risk ------------------------------------------------------------------------
    def realised_net(self) -> float:
        return sum(t["net_pnl"] for t in self.closed)

    def _risk_block(self) -> str | None:
        r = self.cfg.risk
        if self.entry_count >= r.max_trades_per_day:
            return f"max trades/day ({r.max_trades_per_day}) reached"
        if self.realised_net() <= -abs(r.max_loss_per_day):
            return f"daily loss limit ₹{r.max_loss_per_day:,.0f} hit"
        return None

    # -- orders ----------------------------------------------------------------------
    def _enter(self, c, sig_t: datetime, now: datetime):
        block = self._risk_block()
        if block:
            self._event("WARN", f"Entry blocked: {block}")
            return
        try:
            expiry = nearest_expiry(self.trading_day, self.market.expiries(self.trading_day))
            if expiry is None:
                self._event("ERROR", "No expiry available - entry skipped")
                return
            contract = select_contract(self.cfg.strategy_mode, int(c.direction), c.spot_close, expiry)
            tsym = self.market.trading_symbol(contract)
        except Exception as e:  # noqa: BLE001
            self._event("ERROR", f"Contract resolution failed: {e}")
            return
        side = option_side(self.cfg.strategy_mode)
        ref = uuid.uuid4().hex[:12]
        fill = self.broker.execute(contract, tsym, side, self.cfg.qty, ref, now)
        self._log_order(now, ref, fill, tsym, side)
        if not fill.ok or fill.price is None:
            if fill.status == "UNCONFIRMED":
                self.halt(f"Entry order {fill.broker_id} for {tsym} not confirmed - check Groww manually")
            else:
                self._event("ERROR", f"Entry order failed ({fill.status}) for {tsym}")
            return
        self.entry_count += 1
        self.position = {
            "mode": self.cfg.strategy_mode, "direction": int(c.direction), "contract": contract,
            "symbol": contract.groww_symbol, "label": contract.label, "trading_symbol": tsym,
            "strike": contract.strike, "opt_type": contract.opt_type, "expiry": contract.expiry.isoformat(),
            "side": side, "qty": self.cfg.qty, "entry_time": now.isoformat(timespec="seconds"),
            "signal_time": sig_t.isoformat(), "entry_price": fill.price, "spot_entry": float(c.spot_close),
            "entry_no": self.entry_count, "entry_order": fill.broker_id, "ltp": fill.price,
        }
        self._persist_position(self.position, "OPEN")
        self._event("ORDER", f"ENTERED {side} {self.cfg.qty} × {contract.label} @ ₹{fill.price:.2f}",
                    order=fill.broker_id)

    def _exit(self, now: datetime, reason: str) -> bool:
        pos = self.position
        if pos is None:
            return True
        if pos.get("exit_failed"):
            return False  # never re-send after an unconfirmed / failed exit: manual action required
        exit_side = "BUY" if pos["side"] == "SELL" else "SELL"
        fill = None
        for attempt in range(3):
            ref = uuid.uuid4().hex[:12]
            fill = self.broker.execute(pos["contract"], pos["trading_symbol"], exit_side, pos["qty"], ref, now)
            self._log_order(now, ref, fill, pos["trading_symbol"], exit_side)
            if fill.ok and fill.price is not None:
                break
            if fill.status == "UNCONFIRMED":
                break  # do not re-send: the first order may still fill
        if not fill or not fill.ok or fill.price is None:
            pos["exit_failed"] = True
            self._persist_position(pos, "OPEN")
            self.halt(f"EXIT FAILED for {pos['label']} ({fill.status if fill else '?'}). "
                      "MANUAL ACTION REQUIRED: square off in the Groww app.")
            return False
        live = self.cfg.exec_mode == "LIVE"
        rt = round_trip(pos["entry_price"], fill.price, pos["qty"], pos["side"] == "SELL", DEFAULT_FRICTION,
                        fills_are_real=live)
        spot_exit = self.snapshot.get("spot")
        trade = {**{k: v for k, v in pos.items() if k != "contract"},
                 "exit_time": now.isoformat(timespec="seconds"), "exit_price": fill.price,
                 "exit_reason": reason, "exit_order": fill.broker_id, "spot_exit": spot_exit,
                 **rt.to_dict()}
        self.closed.append(trade)
        self.position = None
        self._persist_position(trade, "CLOSED")
        self._event("ORDER", f"EXITED {pos['label']} @ ₹{fill.price:.2f} ({reason}) · net ₹{rt.net_pnl:,.2f}",
                    order=fill.broker_id, net=rt.net_pnl)
        block = self._risk_block()
        if block and self.status == "RUNNING" and "loss" in block:
            self.halt(f"New entries halted: {block}")
        return True

    def _log_order(self, now, ref, fill: Fill, tsym, side):
        if self.journal:
            try:
                self.db.log_order(self.session_id, now, ref, fill.broker_id, tsym, side, self.cfg.qty,
                                  fill.status, fill.price, fill.raw)
            except Exception:  # noqa: BLE001
                log.exception("order journal failed")

    # -- UI state ------------------------------------------------------------------------
    def _mark_to_market(self, now):
        pos = self.position
        if pos is None:
            return
        try:
            ltp = self.market.option_ltp(pos["contract"], now)
        except Exception:  # noqa: BLE001
            ltp = None
        if ltp is not None:
            pos["ltp"] = ltp
            sign = -1 if pos["side"] == "SELL" else 1
            pos["unrealised"] = round(sign * (ltp - pos["entry_price"]) * pos["qty"], 2)

    def _update_snapshot(self, now, spot, bars):
        snap = {"note": None}
        if not spot.empty:
            snap["spot"] = float(spot["close"].iloc[-1])
            snap["spot_time"] = spot.index[-1].isoformat()
        if not bars.empty:
            last = bars.iloc[-1]
            snap.update(ema9=None if pd.isna(last.ema9) else round(float(last.ema9), 2),
                        ema21=None if pd.isna(last.ema21) else round(float(last.ema21), 2),
                        last_bar=bars.index[-1].isoformat())
            day_bars = bars[bars.index.date == now.date()]
            snap["bars"] = [{"time": int(pd.Timestamp(ix).timestamp()), "open": r.open, "high": r.high,
                             "low": r.low, "close": r.close,
                             "ema9": None if pd.isna(r.ema9) else r.ema9,
                             "ema21": None if pd.isna(r.ema21) else r.ema21}
                            for ix, r in day_bars.iterrows()]
        self.snapshot = snap

    def state(self) -> dict:
        pos = None
        if self.position:
            pos = {k: v for k, v in self.position.items() if k != "contract"}
        return {
            "status": self.status, "halt_reason": self.halt_reason, "session_id": self.session_id,
            "config": self.cfg.to_dict(), "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_tick": self.last_tick.isoformat(timespec="seconds") if self.last_tick else None,
            "last_error": self.last_error, "position": pos, "closed": self.closed,
            "realised_net": round(self.realised_net(), 2), "entries": self.entry_count,
            "snapshot": self.snapshot, "events": list(reversed(self.events[-100:])),
        }


# ---------------------------------------------------------------------------
# Replay (paper engine driven by stored candles, minute by minute)
# ---------------------------------------------------------------------------
class ReplayMarketData:
    """Feeds the engine stored 1m candles as if they arrived live. Option 'LTP' at
    minute T is the open of the option candle starting at T - identical to the
    backtest fill rule, so a replay must reproduce the backtest trade-for-trade."""

    def __init__(self, store, day: date):
        self.store = store
        self.day = day
        self._spot = store.spot_1m(day - timedelta(days=14), day)
        self._exp = store.expiries()
        self._opt: dict = {}

    def spot_1m(self, now):
        s = self._spot
        return s[s.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]

    def expiries(self, day):
        return self._exp

    def trading_symbol(self, c):
        return c.groww_symbol

    def option_ltp(self, c, now):
        if c.groww_symbol not in self._opt:
            self._opt[c.groww_symbol] = self.store.option_1m(c.groww_symbol, self.day)
        df = self._opt[c.groww_symbol]
        t = pd.Timestamp(now).floor("min")
        if t in df.index:
            return float(df.at[t, "open"])
        after = df[(df.index > t) & (df.index <= t + pd.Timedelta(minutes=5))]
        if not after.empty:
            return float(after["open"].iloc[0])
        before = df[df.index < t]
        return float(before["close"].iloc[-1]) if not before.empty else None


def replay_day(store, day: date, cfg: LiveConfig) -> dict:
    """Run the paper engine through one stored session, one tick per minute."""
    cfg.exec_mode = "PAPER"
    market = ReplayMarketData(store, day)
    clock_now = {"t": datetime.combine(day, time(9, 15))}
    eng = TradingEngine(cfg, market, PaperBroker(market), db=None, clock=lambda: clock_now["t"], journal=False)
    eng.begin()
    t = datetime.combine(day, time(9, 16))
    end = datetime.combine(day, SQUARE_OFF)
    while t <= end and eng.status in ("RUNNING", "HALTED"):
        clock_now["t"] = t
        eng.tick(t)
        t += timedelta(minutes=1)
    return eng.state()
