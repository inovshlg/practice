import pytest

from app.config import FrictionConfig
from app.strategy.costs import apply_slippage, leg_charges, round_trip

CFG = FrictionConfig()


def test_buy_slippage_is_adverse_and_tick_rounded_up():
    # 100 * 1.005 + 0.5 = 101.0
    assert apply_slippage(100, "BUY", CFG) == 101.0
    # 99.99*1.005+0.5 = 100.98995 -> rounded UP to 101.00
    assert apply_slippage(99.99, "BUY", CFG) == 101.0


def test_sell_slippage_is_adverse_and_tick_rounded_down():
    # 100 * 0.995 - 0.5 = 99.0
    assert apply_slippage(100, "SELL", CFG) == 99.0
    # 37.33*0.995-0.5 = 36.64335 -> down to 36.60
    assert apply_slippage(37.33, "SELL", CFG) == 36.6


def test_sell_price_floored_at_tick():
    assert apply_slippage(0.3, "SELL", CFG) == 0.05
    assert apply_slippage(0.05, "SELL", CFG) == 0.05


def test_slippage_rejects_bad_input():
    with pytest.raises(ValueError):
        apply_slippage(100, "HOLD", CFG)
    with pytest.raises(ValueError):
        apply_slippage(float("nan"), "BUY", CFG)


def test_leg_charges_buy_side():
    c = leg_charges(100.0, 65, "BUY", CFG)  # turnover 6500
    assert c.brokerage == 20
    assert c.stt == 0
    assert c.exchange_txn == pytest.approx(6500 * 0.0003503)
    assert c.sebi_fee == pytest.approx(6500 * 10 / 1e7)
    assert c.stamp_duty == pytest.approx(6500 * 0.00003)
    assert c.gst == pytest.approx((20 + 6500 * 0.0003503 + 6500 * 1e-6) * 0.18)


def test_leg_charges_sell_side():
    c = leg_charges(100.0, 65, "SELL", CFG)
    assert c.stt == pytest.approx(6500 * 0.0015)
    assert c.stamp_duty == 0


def test_round_trip_long_accounting_identity():
    rt = round_trip(100, 120, 65, is_short=False, cfg=CFG)
    assert rt.gross_pnl == pytest.approx(20 * 65)
    assert rt.entry_fill == 101.0 and rt.exit_fill == 118.9  # 120*.995-.5 = 118.9
    assert rt.slippage_cost == pytest.approx((101 - 100 + 120 - 118.9) * 65)
    assert rt.net_pnl == pytest.approx(rt.gross_pnl - rt.slippage_cost - rt.charges.total)
    assert rt.friction == pytest.approx(rt.gross_pnl - rt.net_pnl)


def test_round_trip_short_accounting():
    # sell at 100, buy back at 80 -> profit
    rt = round_trip(100, 80, 65, is_short=True, cfg=CFG)
    assert rt.gross_pnl == pytest.approx(20 * 65)
    assert rt.entry_fill == 99.0          # sell fill
    assert rt.exit_fill == 80.9           # 80*1.005+0.5 = 80.9
    assert rt.net_pnl == pytest.approx((99.0 - 80.9) * 65 - rt.charges.total)
    # STT only on the sell (entry) leg for a short
    assert rt.charges.stt == pytest.approx(99.0 * 65 * 0.0015)


def test_round_trip_real_fills_have_no_simulated_slippage():
    rt = round_trip(100, 110, 65, is_short=False, cfg=CFG, fills_are_real=True)
    assert rt.slippage_cost == 0
    assert rt.entry_fill == 100 and rt.exit_fill == 110


def test_to_dict_fields():
    d = round_trip(50, 40, 130, is_short=False).to_dict()
    for k in ("gross_pnl", "slippage_cost", "charges", "friction", "net_pnl", "entry_fill", "exit_fill"):
        assert k in d
    assert set(d["charges"]) == {"brokerage", "stt", "exchange_txn", "sebi_fee", "stamp_duty", "gst", "total"}
