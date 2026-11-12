"""Slippage and Indian index-options cost stack.

Conventions
-----------
* Slippage is always adverse: a BUY fills higher, a SELL fills lower.
    buy_fill  = raw * (1 + pct) + points
    sell_fill = max(TICK, raw * (1 - pct) - points)      # floored at the 0.05 tick
  Fills are rounded to the 0.05 tick (buys rounded up, sells rounded down) so
  rounding never works in the trader's favour.
* Charges are computed per leg (one executed order) and summed for a round trip.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from ..config import DEFAULT_FRICTION, TICK, FrictionConfig


def _round_up_tick(px: float) -> float:
    return round(math.ceil(round(px / TICK, 6)) * TICK, 2)


def _round_down_tick(px: float) -> float:
    return round(math.floor(round(px / TICK, 6)) * TICK, 2)


def apply_slippage(raw_price: float, side: str, cfg: FrictionConfig = DEFAULT_FRICTION) -> float:
    """Return the fill price after adverse slippage. side is 'BUY' or 'SELL'."""
    if raw_price is None or raw_price != raw_price:  # None / NaN
        raise ValueError("raw_price must be a number")
    if side == "BUY":
        return max(TICK, _round_up_tick(raw_price * (1 + cfg.slippage_pct) + cfg.slippage_points))
    if side == "SELL":
        return max(TICK, _round_down_tick(raw_price * (1 - cfg.slippage_pct) - cfg.slippage_points))
    raise ValueError(f"side must be BUY or SELL, got {side!r}")


@dataclass
class LegCharges:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi_fee: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi_fee + self.stamp_duty + self.gst

    def __add__(self, other: "LegCharges") -> "LegCharges":
        return LegCharges(**{k: getattr(self, k) + getattr(other, k) for k in asdict(self)})

    def rounded(self) -> dict:
        d = {k: round(v, 2) for k, v in asdict(self).items()}
        d["total"] = round(self.total, 2)
        return d


def leg_charges(price: float, qty: int, side: str, cfg: FrictionConfig = DEFAULT_FRICTION) -> LegCharges:
    turnover = price * qty
    brokerage = cfg.brokerage_per_order if qty > 0 else 0.0
    stt = turnover * cfg.stt_sell_pct if side == "SELL" else 0.0
    exch = turnover * cfg.exchange_txn_pct
    sebi = turnover * cfg.sebi_fee_per_crore / 1e7
    stamp = turnover * cfg.stamp_duty_buy_pct if side == "BUY" else 0.0
    gst = (brokerage + exch + sebi) * cfg.gst_pct
    return LegCharges(brokerage, stt, exch, sebi, stamp, gst)


@dataclass
class RoundTrip:
    """Full accounting of one trade (entry leg + exit leg)."""
    entry_raw: float
    exit_raw: float
    entry_fill: float
    exit_fill: float
    qty: int
    is_short: bool
    gross_pnl: float          # on raw (mid / candle) prices, no friction
    slippage_cost: float      # PnL lost to slippage
    charges: LegCharges
    net_pnl: float

    @property
    def friction(self) -> float:
        return self.slippage_cost + self.charges.total

    def to_dict(self) -> dict:
        return {
            "entry_raw": self.entry_raw, "exit_raw": self.exit_raw,
            "entry_fill": self.entry_fill, "exit_fill": self.exit_fill,
            "qty": self.qty, "is_short": self.is_short,
            "gross_pnl": round(self.gross_pnl, 2),
            "slippage_cost": round(self.slippage_cost, 2),
            "charges": self.charges.rounded(),
            "friction": round(self.friction, 2),
            "net_pnl": round(self.net_pnl, 2),
        }


def round_trip(entry_raw: float, exit_raw: float, qty: int, is_short: bool,
               cfg: FrictionConfig = DEFAULT_FRICTION, *, fills_are_real: bool = False) -> RoundTrip:
    """Compute gross / slippage / charges / net for a round trip.

    For a long option: BUY at entry, SELL at exit. For a short: SELL at entry, BUY at exit.
    When ``fills_are_real`` is True (live trading) the given prices are the broker's
    actual fills, so no simulated slippage is added.
    """
    entry_side, exit_side = ("SELL", "BUY") if is_short else ("BUY", "SELL")
    if fills_are_real:
        entry_fill, exit_fill = entry_raw, exit_raw
    else:
        entry_fill = apply_slippage(entry_raw, entry_side, cfg)
        exit_fill = apply_slippage(exit_raw, exit_side, cfg)
    sign = -1 if is_short else 1
    gross = sign * (exit_raw - entry_raw) * qty
    after_slip = sign * (exit_fill - entry_fill) * qty
    charges = leg_charges(entry_fill, qty, entry_side, cfg) + leg_charges(exit_fill, qty, exit_side, cfg)
    net = after_slip - charges.total
    return RoundTrip(entry_raw, exit_raw, entry_fill, exit_fill, qty, is_short,
                     gross, gross - after_slip, charges, net)
