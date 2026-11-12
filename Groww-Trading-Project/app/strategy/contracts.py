"""Option contract selection (strike, type, expiry) - shared by backtest and live."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..config import ITM_DEPTH, STRIKE_STEP, UNDERLYING
from .indicators import atm_strike

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass(frozen=True)
class Contract:
    expiry: date
    strike: int
    opt_type: str  # CE / PE

    @property
    def groww_symbol(self) -> str:
        """Groww historical-data symbol, e.g. NSE-NIFTY-30Sep25-24650-PE."""
        e = self.expiry
        return f"NSE-{UNDERLYING}-{e.day:02d}{MONTHS[e.month - 1]}{e.year % 100:02d}-{self.strike}-{self.opt_type}"

    @property
    def label(self) -> str:
        return f"{UNDERLYING} {self.expiry:%d %b %y} {self.strike} {self.opt_type}"


def option_side(mode: str) -> str:
    """BUY for the two long modes, SELL for ATM_SELL."""
    return "SELL" if mode == "ATM_SELL" else "BUY"


def select_contract(mode: str, direction: int, spot: float, expiry: date,
                    step: int = STRIKE_STEP, depth: int = ITM_DEPTH) -> Contract:
    """Pick the contract for a signal.

    Bullish (+1): buy CE (ATM / ITM5) or SELL ATM PE.
    Bearish (-1): buy PE (ATM / ITM5) or SELL ATM CE.
    ITM5 CE strike = ATM - 5*step; ITM5 PE strike = ATM + 5*step.
    The spot used is the CLOSE of the signal bar (known when the signal fires).
    """
    atm = atm_strike(spot, step)
    if mode == "ATM_SELL":
        return Contract(expiry, atm, "PE" if direction > 0 else "CE")
    opt = "CE" if direction > 0 else "PE"
    if mode == "ATM_BUY":
        return Contract(expiry, atm, opt)
    if mode == "ITM5_BUY":
        strike = atm - depth * step if opt == "CE" else atm + depth * step
        return Contract(expiry, strike, opt)
    raise ValueError(f"unknown mode {mode}")


def nearest_expiry(day: date, expiries: list[date]) -> date | None:
    """Nearest expiry on or after ``day`` (0-DTE is traded on expiry day)."""
    future = [e for e in expiries if e >= day]
    return min(future) if future else None
