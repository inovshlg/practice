"""Live / paper engine: decisions, safety rails, broker handling, journal, replay parity."""
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

import app.live.engine as eng_mod
from app.config import RiskLimits
from app.live.engine import (Fill, GrowwBroker, LiveConfig, PaperBroker, ReplayMarketData, TradingEngine,
                             replay_day)

from .conftest import MemStore, flat_ohlc, minute_index
from .test_backtest import EXP, X, make_store, opt_price

D1 = date(2026, 3, 2)


class FakeMarket:
    def __init__(self, day=D1):
        self.day = day
        self.spot = flat_ohlc(minute_index(day), np.full(375, 25010.0))
        self.ltp_calls = 0

    def spot_1m(self, now):
        return self.spot[self.spot.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]

    def expiries(self, day):
        return EXP

    def trading_symbol(self, c):
        return f"NIFTY{c.strike}{c.opt_type}"

    def option_ltp(self, c, now):
        self.ltp_calls += 1
        return opt_price(pd.Timestamp(now).floor("min"))


class ScriptedBroker:
    """Returns queued fills; defaults to a fill at the market LTP."""

    def __init__(self, market, script=None):
        self.market = market
        self.script = list(script or [])
        self.calls = []

    def execute(self, contract, tsym, side, qty, ref, now):
        self.calls.append((side, tsym, qty))
        if self.script:
            f = self.script.pop(0)
            if f is not None:
                return f
        return Fill(True, self.market.option_ltp(contract, now), "EXECUTED", f"OID{len(self.calls)}")


@pytest.fixture
def fake_crosses(monkeypatch):
    holder = {"rows": []}

    def fake_signal_frame(df1m, minutes, now=None):
        rows = [r for r in holder["rows"] if now is None or r["signal_time"] <= pd.Timestamp(now)]
        cols = ["bar_start", "signal_time", "direction", "spot_close", "ema9", "ema21", "day", "day_cross_no"]
        return pd.DataFrame(columns=["ema9", "ema21"]), pd.DataFrame(rows, columns=cols)
    monkeypatch.setattr(eng_mod, "signal_frame", fake_signal_frame)
    return holder


def run_day(engine, start="09:16", end="15:00"):
    t = datetime.combine(engine.trading_day, datetime.strptime(start, "%H:%M").time())
    stop = datetime.combine(engine.trading_day, datetime.strptime(end, "%H:%M").time())
    while t <= stop:
        engine.clock_now["t"] = t
        engine.tick(t)
        t += timedelta(minutes=1)


def make_engine(start="09:15", broker=None, market=None, **cfg):
    market = market or FakeMarket()
    clock_now = {"t": datetime.combine(D1, datetime.strptime(start, "%H:%M").time())}
    c = LiveConfig(**{"strategy_mode": "ATM_BUY", "timeframe": 15, **cfg})
    e = TradingEngine(c, market, broker or ScriptedBroker(market), db=None, clock=lambda: clock_now["t"],
                      journal=False)
    e.clock_now = clock_now
    e.begin()
    return e


def test_paper_like_flow_enter_reverse_squareoff(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1), X(D1, "11:00", -1)]
    e = make_engine()
    run_day(e)
    assert len(e.closed) == 2 and e.position is None
    a, b = e.closed
    assert a["exit_reason"] == "REVERSAL" and a["entry_price"] == opt_price(pd.Timestamp("2026-03-02 10:00"))
    assert b["opt_type"] == "PE" and b["exit_reason"] == "SQUARE_OFF_15:00"
    assert e.status == "DONE"


def test_crosses_closed_before_start_are_ignored(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1)]
    e = make_engine(start="11:00")
    run_day(e, start="11:00")
    assert e.closed == [] and e.position is None
    assert any("Ignoring cross" in ev["message"] for ev in e.events)


def test_max_trades_per_day_blocks_new_entries(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1), X(D1, "11:00", -1), X(D1, "12:00", 1)]
    e = make_engine(risk=RiskLimits(max_trades_per_day=2, max_loss_per_day=1e9))
    run_day(e)
    assert e.entry_count == 2 and len(e.closed) == 2
    assert any("max trades/day" in ev["message"] for ev in e.events)


def test_daily_loss_limit_halts_entries(fake_crosses):
    # option price rises 1/min: a short (ATM_SELL) loses on every trade
    fake_crosses["rows"] = [X(D1, "10:00", 1), X(D1, "12:00", -1), X(D1, "13:00", 1)]
    e = make_engine(strategy_mode="ATM_SELL", risk=RiskLimits(max_trades_per_day=10, max_loss_per_day=1000))
    run_day(e)
    assert len(e.closed) == 1 and e.status == "HALTED"
    assert e.closed[0]["net_pnl"] < -1000
    assert "loss limit" in e.halt_reason


def test_rejected_entry_leaves_engine_flat(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1)]
    m = FakeMarket()
    e = make_engine(market=m, broker=ScriptedBroker(m, [Fill(False, None, "REJECTED", "OID1")]))
    run_day(e, end="10:05")
    assert e.position is None and e.entry_count == 0 and e.status == "RUNNING"
    assert any("REJECTED" in ev["message"] for ev in e.events)


def test_unconfirmed_exit_halts_and_never_resends(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1), X(D1, "11:00", -1)]
    m = FakeMarket()
    b = ScriptedBroker(m, [None, Fill(False, None, "UNCONFIRMED", "OID2")])
    e = make_engine(market=m, broker=b)
    run_day(e)
    assert e.status == "HALTED" and "MANUAL ACTION" in e.halt_reason
    assert e.position is not None and e.position["exit_failed"]
    sells = [c for c in b.calls if c[0] == "SELL"]
    assert len(sells) == 1               # not re-sent at 11:00 retry, nor at 15:00 square-off
    assert len(b.calls) == 2             # no reverse entry stacked on top of a stuck position


def test_failed_exit_is_retried_then_succeeds(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1)]
    m = FakeMarket()
    b = ScriptedBroker(m, [None, Fill(False, None, "REJECTED"), Fill(False, None, "REJECTED")])
    e = make_engine(market=m, broker=b)
    run_day(e)
    assert len(e.closed) == 1 and e.position is None
    assert len(b.calls) == 4


def test_stop_flattens(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1)]
    e = make_engine()
    run_day(e, end="10:30")
    assert e.position is not None
    e.stop(flatten=True)
    assert e.position is None and e.closed[0]["exit_reason"] == "MANUAL_STOP" and e.status == "STOPPED"


def test_stale_data_blocks_entries(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1)]
    m = FakeMarket()
    m.spot = m.spot[m.spot.index < pd.Timestamp("2026-03-02 09:40")]   # feed froze at 09:40
    e = make_engine(market=m)
    run_day(e, end="10:10")
    assert e.position is None
    assert any("stale" in ev["message"] for ev in e.events)


def test_live_mode_books_real_fills_without_simulated_slippage(fake_crosses):
    fake_crosses["rows"] = [X(D1, "10:00", 1)]
    e = make_engine(exec_mode="LIVE")
    run_day(e)
    t = e.closed[0]
    assert t["slippage_cost"] == 0 and t["entry_fill"] == t["entry_price"]


def test_config_validation():
    for bad in (dict(exec_mode="X"), dict(strategy_mode="Y"), dict(timeframe=7), dict(lots=0), dict(lots=99),
                dict(entry_start="12:00", entry_cutoff="11:00"), dict(entry_cutoff="15:10")):
        with pytest.raises(ValueError):
            LiveConfig(**bad).validate()


# ------------------------------------------------------------------ GrowwBroker
class FakeClient:
    def __init__(self, statuses, place_exc=None):
        self.statuses = list(statuses)
        self.place_exc = place_exc
        self.placed = []

    def place_market_order(self, tsym, qty, side, ref):
        if self.place_exc:
            raise self.place_exc
        self.placed.append((tsym, qty, side, ref))
        return {"groww_order_id": "G1", "order_status": "NEW"}

    def order_detail(self, oid):
        return self.statuses.pop(0)


def test_groww_broker_waits_for_fill():
    c = FakeClient([{"order_status": "OPEN"}, {"order_status": "EXECUTED", "average_fill_price": 123.45}])
    f = GrowwBroker(c, timeout_s=10, sleep=lambda s: None).execute(None, "NIFTYX", "BUY", 65, "r1", None)
    assert f.ok and f.price == 123.45 and f.broker_id == "G1"
    assert c.placed == [("NIFTYX", 65, "BUY", "r1")]


def test_groww_broker_rejection_and_timeout_and_exception():
    f = GrowwBroker(FakeClient([{"order_status": "REJECTED"}]), sleep=lambda s: None).execute(None, "X", "BUY", 65, "r", None)
    assert not f.ok and f.status == "REJECTED"
    f = GrowwBroker(FakeClient([{"order_status": "OPEN"}] * 5), timeout_s=3, sleep=lambda s: None).execute(None, "X", "BUY", 65, "r", None)
    assert not f.ok and f.status == "UNCONFIRMED"
    f = GrowwBroker(FakeClient([], place_exc=RuntimeError("boom")), sleep=lambda s: None).execute(None, "X", "BUY", 65, "r", None)
    assert not f.ok and f.status.startswith("PLACE_FAILED")


# ------------------------------------------------------------------ journal + recovery
def test_journal_and_readopt_open_position(fake_crosses, test_db):
    fake_crosses["rows"] = [X(D1, "10:00", 1)]
    m = FakeMarket()
    clock = {"t": datetime(2026, 3, 2, 9, 15)}
    e = TradingEngine(LiveConfig(strategy_mode="ATM_BUY"), m, ScriptedBroker(m), test_db, clock=lambda: clock["t"])
    e.begin()
    for t in pd.date_range("2026-03-02 09:16", "2026-03-02 10:05", freq="1min"):
        clock["t"] = t.to_pydatetime()
        e.tick(clock["t"])
    assert e.position is not None
    assert test_db.open_positions(D1, "PAPER")
    assert test_db.orders(limit=10)[0]["side"] == "BUY"
    assert any(ev["level"] == "SIGNAL" for ev in test_db.events())
    # simulate a crash: new engine instance re-adopts the open position and closes it at 15:00
    clock["t"] = datetime(2026, 3, 2, 10, 30)
    e2 = TradingEngine(LiveConfig(strategy_mode="ATM_BUY"), m, ScriptedBroker(m), test_db, clock=lambda: clock["t"])
    e2.begin()
    assert e2.position is not None and e2.position["strike"] == 25000
    clock["t"] = datetime(2026, 3, 2, 15, 0)
    e2.tick(clock["t"])
    assert e2.position is None and not test_db.open_positions(D1, "PAPER")
    closed = [p for p in test_db.positions(D1) if p["status"] == "CLOSED"]
    assert closed[0]["exit_reason"] == "SQUARE_OFF_15:00"


# ------------------------------------------------------------------ replay parity with backtest
def test_replay_reproduces_backtest_trade_for_trade():
    from app.data_loader import DemoSource
    from app.strategy.backtest import BacktestParams, run_backtest
    from app.strategy.contracts import Contract

    src = DemoSource(seed=5)
    d0, d1 = date(2026, 4, 6), date(2026, 4, 17)
    st = MemStore(src.spot(d0 - timedelta(days=14), d1), expiries=src.expiries(d0, d1))
    cache = {}

    def opt(sym, day):
        if (sym, day) not in cache:
            p = sym.split("-")
            cache[(sym, day)] = src.option(Contract(datetime.strptime(p[2], "%d%b%y").date(), int(p[3]), p[4]), day)
        return cache[(sym, day)]
    st.default_option = opt
    compared = 0
    for tf, mode in [(5, "ATM_BUY"), (15, "ITM5_BUY"), (30, "ATM_SELL"), (5, "ATM_SELL")]:
        r = run_backtest(st, BacktestParams(d0, d1, timeframe=tf, modes=(mode,), entry_start=time(10, 0),
                                            entry_cutoff=time(13, 30)))
        for day in sorted({t["day"] for t in r["trades"]}):
            bt = [(t["entry_time"], t["exit_time"], t["entry_raw"], t["exit_raw"], t["exit_reason"], t["net_pnl"])
                  for t in r["trades"] if t["day"] == day]
            cfg = LiveConfig(strategy_mode=mode, timeframe=tf, entry_start="10:00", entry_cutoff="13:30",
                             risk=RiskLimits(max_trades_per_day=99, max_loss_per_day=1e12))
            s = replay_day(st, date.fromisoformat(day), cfg)
            lv = [(t["entry_time"], t["exit_time"], t["entry_price"], t["exit_price"], t["exit_reason"], t["net_pnl"])
                  for t in s["closed"]]
            assert bt == lv, (tf, mode, day)
            compared += 1
    assert compared >= 10
