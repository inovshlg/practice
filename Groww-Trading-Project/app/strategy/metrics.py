"""Performance metrics on a list of trade dicts (net_pnl / gross_pnl / friction)."""
from __future__ import annotations

import math


def _r(x, n=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(x, n)


def compute_metrics(trades: list[dict], capital: float) -> dict:
    """Metrics definitions
    * win rate      = trades with net_pnl > 0 / trades (net 0 counts as a loss)
    * profit factor = sum(winning net) / |sum(losing net)|  (None if no losers)
    * payoff ratio  = avg winning net / |avg losing net|
    * expectancy    = average net PnL per trade (₹)
    * max drawdown  = largest peak-to-trough fall of the closed-trade equity curve
                      (capital + cumulative net PnL, trades in exit order).
                      % is relative to the peak equity at that time.
    """
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "gross_pnl": 0.0, "net_pnl": 0.0,
                "friction": 0.0, "slippage": 0.0, "charges": 0.0, "profit_factor": None,
                "payoff_ratio": None, "expectancy": None, "avg_win": None, "avg_loss": None,
                "max_dd": 0.0, "max_dd_pct": 0.0, "whipsaws": 0, "best": None, "worst": None,
                "equity": []}
    ordered = sorted(trades, key=lambda t: t["exit_time"])
    nets = [t["net_pnl"] for t in ordered]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    eq, peak, max_dd, max_dd_pct = capital, capital, 0.0, 0.0
    curve = []
    for t in ordered:
        eq += t["net_pnl"]
        peak = max(peak, eq)
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            max_dd_pct = max(max_dd_pct, dd / peak * 100)
        curve.append({"t": t["exit_time"], "equity": round(eq, 2)})
    avg_win = gross_win / len(wins) if wins else None
    avg_loss = gross_loss / len(losses) if losses else None
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _r(len(wins) / n * 100, 1),
        "gross_pnl": _r(sum(t["gross_pnl"] for t in trades)),
        "net_pnl": _r(sum(nets)),
        "friction": _r(sum(t["friction"] for t in trades)),
        "slippage": _r(sum(t["slippage_cost"] for t in trades)),
        "charges": _r(sum(t["charges"]["total"] for t in trades)),
        "profit_factor": _r(gross_win / gross_loss) if gross_loss > 0 else None,
        "payoff_ratio": _r(avg_win / avg_loss) if (avg_win is not None and avg_loss) else None,
        "expectancy": _r(sum(nets) / n),
        "avg_win": _r(avg_win),
        "avg_loss": _r(-avg_loss) if avg_loss is not None else None,
        "max_dd": _r(max_dd),
        "max_dd_pct": _r(max_dd_pct),
        "whipsaws": sum(1 for t in trades if t.get("whipsaw")),
        "best": _r(max(nets)),
        "worst": _r(min(nets)),
        "equity": curve,
    }
