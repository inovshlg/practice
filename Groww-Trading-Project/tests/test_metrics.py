import pytest

from app.strategy.metrics import compute_metrics


def T(net, gross=None, i=0, whipsaw=False):
    gross = net + 10 if gross is None else gross
    return {"net_pnl": net, "gross_pnl": gross, "friction": gross - net, "slippage_cost": 4,
            "charges": {"total": gross - net - 4}, "exit_time": f"2026-03-02T10:{i:02d}:00", "whipsaw": whipsaw}


def test_empty():
    m = compute_metrics([], 100000)
    assert m["trades"] == 0 and m["win_rate"] is None and m["profit_factor"] is None and m["max_dd"] == 0


def test_basic_metrics():
    trades = [T(100, i=0), T(-50, i=1), T(200, i=2), T(-100, i=3, whipsaw=True)]
    m = compute_metrics(trades, 1000)
    assert m["trades"] == 4 and m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate"] == 50.0
    assert m["net_pnl"] == 150
    assert m["gross_pnl"] == 190
    assert m["profit_factor"] == pytest.approx(300 / 150)
    assert m["payoff_ratio"] == pytest.approx(150 / 75)
    assert m["expectancy"] == pytest.approx(37.5)
    assert m["avg_loss"] == -75
    assert m["whipsaws"] == 1
    assert m["best"] == 200 and m["worst"] == -100


def test_drawdown_uses_exit_order_and_peak():
    # equity: 1000 -> 1100 -> 1050 -> 1250 -> 1150 -> 950
    trades = [T(100, i=0), T(-50, i=1), T(200, i=2), T(-100, i=3), T(-200, i=4)]
    m = compute_metrics(list(reversed(trades)), 1000)   # input order must not matter
    assert m["max_dd"] == 300
    assert m["max_dd_pct"] == pytest.approx(300 / 1250 * 100, abs=0.01)
    assert [p["equity"] for p in m["equity"]] == [1100, 1050, 1250, 1150, 950]


def test_zero_pnl_counts_as_loss_and_no_losers_pf_none():
    m = compute_metrics([T(0), T(10, i=1)], 1000)
    assert m["wins"] == 1 and m["losses"] == 1
    m2 = compute_metrics([T(10), T(20, i=1)], 1000)
    assert m2["profit_factor"] is None and m2["payoff_ratio"] is None
