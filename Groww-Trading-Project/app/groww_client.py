"""Thin wrapper around the official ``growwapi`` SDK.

Auth: TOTP flow. ``keys.txt`` (never committed) holds the TOTP API key and the
TOTP secret, either as

    API_KEY=eyJraWQiOi...
    TOTP_SECRET=JBSWY3DPEHPK3PXP

or simply two lines: key on line 1, secret on line 2.  A fresh 6-digit code is
generated with pyotp and exchanged for an access token, which is cached on disk
until 06:00 IST the next day (Groww's daily token expiry).
"""
from __future__ import annotations

import json
import logging
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import SETTINGS
from .timeutil import now_ist

log = logging.getLogger(__name__)


class GrowwAuthError(RuntimeError):
    pass


def read_keys(path: Path) -> tuple[str, str]:
    if not path.exists():
        raise GrowwAuthError(f"keys file not found: {path}. Create it with API_KEY= and TOTP_SECRET= lines.")
    kv, bare = {}, []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and line.split("=", 1)[0].strip().isupper():
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip().strip('"').strip("'")
        elif ":" in line and line.split(":", 1)[0].strip().upper() in ("API_KEY", "TOTP_SECRET", "KEY", "SECRET"):
            k, v = line.split(":", 1)
            kv[k.strip().upper()] = v.strip()
        else:
            bare.append(line)
    key = kv.get("API_KEY") or kv.get("TOTP_KEY") or kv.get("KEY") or (bare[0] if bare else None)
    secret = kv.get("TOTP_SECRET") or kv.get("SECRET") or (bare[1] if len(bare) > 1 else None)
    if not key or not secret:
        raise GrowwAuthError("keys.txt must contain the TOTP API key and the TOTP secret")
    return key, secret.replace(" ", "")


class RateLimiter:
    def __init__(self, per_second: float):
        self.min_gap = 1.0 / per_second
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            gap = _time.monotonic() - self._last
            if gap < self.min_gap:
                _time.sleep(self.min_gap - gap)
            self._last = _time.monotonic()


def parse_candles(payload) -> pd.DataFrame:
    """Normalise a Groww candle response into a DataFrame indexed by IST-naive time."""
    cols = ["open", "high", "low", "close", "volume", "oi"]
    candles = payload.get("candles") if isinstance(payload, dict) else payload
    if not candles:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], name="ts"))
    rows = []
    for c in candles:
        ts = c[0]
        if isinstance(ts, (int, float)):
            sec = ts / 1000 if ts > 1e11 else ts
            t = pd.Timestamp(sec, unit="s", tz="UTC").tz_convert("Asia/Kolkata").tz_localize(None)
        else:
            t = pd.Timestamp(ts)
            if t.tzinfo is not None:
                t = t.tz_convert("Asia/Kolkata").tz_localize(None)
        vals = list(c[1:7]) + [None] * (6 - len(c[1:7]))
        rows.append([t] + vals)
    df = pd.DataFrame(rows, columns=["ts"] + cols).set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


class GrowwClient:
    """Lazy, thread-safe Groww session."""

    def __init__(self, keys_file: Path | None = None, token_cache: Path | None = None):
        self.keys_file = keys_file or SETTINGS.keys_file
        self.token_cache = token_cache or SETTINGS.token_cache_file
        self._api = None
        self._lock = threading.Lock()
        self.limiter = RateLimiter(per_second=8)

    # -- auth --------------------------------------------------------------
    def _cached_token(self) -> str | None:
        try:
            data = json.loads(self.token_cache.read_text())
            if datetime.fromisoformat(data["expires"]) > now_ist().replace(tzinfo=None):
                return data["token"]
        except Exception:
            return None
        return None

    def _login(self) -> str:
        import pyotp
        from growwapi import GrowwAPI

        key, secret = read_keys(self.keys_file)
        totp = pyotp.TOTP(secret).now()
        try:
            token = GrowwAPI.get_access_token(api_key=key, totp=totp)
        except Exception as e:  # noqa: BLE001
            raise GrowwAuthError(f"Groww login failed: {e}") from e
        now = now_ist().replace(tzinfo=None)
        expires = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        if now.hour < 6:
            expires = now.replace(hour=6, minute=0, second=0, microsecond=0)
        try:
            self.token_cache.write_text(json.dumps({"token": token, "expires": expires.isoformat()}))
            self.token_cache.chmod(0o600)
        except OSError:
            log.warning("could not cache token")
        return token

    @property
    def api(self):
        with self._lock:
            if self._api is None:
                from growwapi import GrowwAPI
                token = self._cached_token() or self._login()
                self._api = GrowwAPI(token)
            return self._api

    def reset(self):
        with self._lock:
            self._api = None
            try:
                self.token_cache.unlink()
            except OSError:
                pass

    def call(self, fn_name: str, *args, retries: int = 3, **kwargs):
        """Rate-limited call with retry + one re-login on auth failure."""
        last = None
        for attempt in range(retries):
            self.limiter.wait()
            try:
                return getattr(self.api, fn_name)(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                last = e
                name = type(e).__name__
                if "Authentication" in name or "Unauthor" in str(e):
                    self.reset()
                elif "RateLimit" in name or "429" in str(e):
                    _time.sleep(1.5 * (attempt + 1))
                elif "NotFound" in name or "BadRequest" in name:
                    raise
                else:
                    _time.sleep(0.5 * (attempt + 1))
        raise last

    # -- account -------------------------------------------------------------
    def profile(self) -> dict:
        return self.call("get_user_profile")

    def margins(self) -> dict:
        return self.call("get_available_margin_details")

    def positions(self) -> dict:
        return self.call("get_positions_for_user")

    def holdings(self) -> dict:
        return self.call("get_holdings_for_user")

    def orders(self) -> dict:
        return self.call("get_order_list")

    # -- market data -----------------------------------------------------------
    def candles(self, groww_symbol: str, start: datetime, end: datetime, segment: str,
                interval: str = "1minute") -> pd.DataFrame:
        api = self.api
        seg = api.SEGMENT_CASH if segment == "CASH" else api.SEGMENT_FNO
        payload = self.call("get_historical_candles", exchange=api.EXCHANGE_NSE, segment=seg,
                            groww_symbol=groww_symbol,
                            start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
                            end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
                            candle_interval=interval)
        return parse_candles(payload)

    def expiries(self, year: int, month: int | None = None) -> list[date]:
        res = self.call("get_expiries", exchange="NSE", underlying_symbol="NIFTY", year=year, month=month)
        raw = res.get("expiries", res) if isinstance(res, dict) else res
        return sorted({pd.Timestamp(x).date() for x in raw})

    def contracts(self, expiry: date) -> list[str]:
        res = self.call("get_contracts", exchange="NSE", underlying_symbol="NIFTY",
                        expiry_date=expiry.isoformat())
        return res.get("contracts", res) if isinstance(res, dict) else res

    def ltp(self, trading_symbols: list[str], segment: str) -> dict[str, float]:
        """LTP for trading symbols (without exchange prefix). Returns {trading_symbol: price}."""
        api = self.api
        seg = api.SEGMENT_CASH if segment == "CASH" else api.SEGMENT_FNO
        keys = tuple(f"NSE_{s}" for s in trading_symbols)
        res = self.call("get_ltp", segment=seg, exchange_trading_symbols=keys)
        return {k.split("_", 1)[1]: float(v) for k, v in (res or {}).items() if v is not None}

    def option_trading_symbol(self, expiry: date, strike: int, opt_type: str, groww_symbol: str) -> str:
        """Resolve the exchange trading symbol of a NIFTY option via the instruments master."""
        df = self.api.get_all_instruments()
        try:
            m = df[(df["underlying_symbol"] == "NIFTY") & (df["instrument_type"] == opt_type)
                   & (pd.to_datetime(df["expiry_date"]).dt.date == expiry)
                   & (pd.to_numeric(df["strike_price"]).round() == strike)]
            if not m.empty:
                return str(m.iloc[0]["trading_symbol"])
        except KeyError:
            pass
        m = df[df["groww_symbol"] == groww_symbol]
        if m.empty:
            raise LookupError(f"instrument not found: {groww_symbol}")
        return str(m.iloc[0]["trading_symbol"])

    # -- orders ------------------------------------------------------------------
    def place_market_order(self, trading_symbol: str, qty: int, side: str, ref: str) -> dict:
        api = self.api
        return self.call("place_order", retries=1, validity=api.VALIDITY_DAY, exchange=api.EXCHANGE_NSE,
                         order_type=api.ORDER_TYPE_MARKET, product=api.PRODUCT_MIS, quantity=qty,
                         segment=api.SEGMENT_FNO, trading_symbol=trading_symbol,
                         transaction_type=api.TRANSACTION_TYPE_BUY if side == "BUY" else api.TRANSACTION_TYPE_SELL,
                         order_reference_id=ref)

    def order_detail(self, groww_order_id: str) -> dict:
        return self.call("get_order_detail", segment=self.api.SEGMENT_FNO, groww_order_id=groww_order_id)


_client: GrowwClient | None = None


def get_client() -> GrowwClient:
    global _client
    if _client is None:
        _client = GrowwClient()
    return _client
