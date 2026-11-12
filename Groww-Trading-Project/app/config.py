"""Central configuration. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = _env("DATABASE_URL", "postgresql://groww:groww@localhost:5432/groww_trading")
    keys_file: Path = Path(_env("GROWW_KEYS_FILE", str(PROJECT_ROOT / "keys.txt")))
    token_cache_file: Path = Path(_env("GROWW_TOKEN_CACHE", str(PROJECT_ROOT / ".groww_token.json")))
    secret_key: str = _env("FLASK_SECRET_KEY", "change-me-in-production")
    host: str = _env("FLASK_HOST", "127.0.0.1")
    port: int = int(_env("FLASK_PORT", "5000"))


SETTINGS = Settings()

# ---------------------------------------------------------------------------
# Market constants
# ---------------------------------------------------------------------------
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
SQUARE_OFF = time(15, 0)

UNDERLYING = "NIFTY"
SPOT_GROWW_SYMBOL = "NSE-NIFTY"
STRIKE_STEP = 50
LOT_SIZE = 65
DEFAULT_LOTS = 1
ITM_DEPTH = 5  # "Deep ITM" = 5 strikes in the money

TIMEFRAMES = (5, 15, 30)
ENTRY_START_CHOICES = (time(9, 30), time(10, 0), time(10, 30))
ENTRY_CUTOFF_CHOICES = tuple(time(h, m) for h, m in
                             [(11, 30), (12, 0), (12, 30), (13, 0), (13, 30), (14, 0), (14, 30)])

MODES = ("ITM5_BUY", "ATM_BUY", "ATM_SELL")
MODE_LABELS = {"ITM5_BUY": "Deep ITM buy (ITM 5)", "ATM_BUY": "ATM buy", "ATM_SELL": "ATM sell"}

TICK = 0.05


@dataclass(frozen=True)
class FrictionConfig:
    """Slippage + full Indian options cost stack (NSE index options, Groww).

    Rates are as published for FY 2026-27; edit here if they change.
    """
    slippage_pct: float = 0.005          # 0.5 % of premium, adverse
    slippage_points: float = 0.5         # plus fixed points, adverse
    brokerage_per_order: float = 20.0    # Groww flat ₹20 per executed order (F&O)
    stt_sell_pct: float = 0.0015         # STT 0.15 % on sell-side premium (Budget 2026, eff. 1-Apr-2026)
    exchange_txn_pct: float = 0.0003503  # NSE options transaction charge 0.03503 % of premium
    sebi_fee_per_crore: float = 10.0     # ₹10 per crore of turnover
    stamp_duty_buy_pct: float = 0.00003  # 0.003 % on buy side
    gst_pct: float = 0.18                # 18 % on brokerage + exchange charges + SEBI fee


DEFAULT_FRICTION = FrictionConfig()
DEFAULT_CAPITAL = 200_000.0  # used only for drawdown % denominator


@dataclass(frozen=True)
class RiskLimits:
    """Guard rails for the live / paper engine."""
    max_trades_per_day: int = 6
    max_loss_per_day: float = 5_000.0
    max_lots: int = 4
    order_fill_timeout_s: int = 30
    data_stale_after_s: int = 180


DEFAULT_RISK = RiskLimits()
