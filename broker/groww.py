"""
Groww API Broker — wraps growwapi SDK with paper-trade fallback.
"""

import os
import pyotp
from dotenv import load_dotenv

load_dotenv()

try:
    from growwapi import GrowwAPI
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


class GrowwConnector:
    """Thin wrapper around GrowwAPI with mock fallback."""

    def __init__(self) -> None:
        self._api = None
        self._ok  = False

        token = os.getenv("GROWW_ACCESS_TOKEN", "").strip()
        # Auto-detect JWT pasted in GROWW_TOTP_SECRET by mistake
        if not token:
            cand = os.getenv("GROWW_TOTP_SECRET", "").strip()
            if cand and len(cand) > 100 and cand.upper().startswith("EY"):
                token = cand
        if token:
            self._init(token)

    @property
    def is_connected(self) -> bool:
        return self._ok

    # ── Auth ──────────────────────────────────────────────────────────────────

    def auth_token(self, token: str) -> tuple[bool, str]:
        try:
            self._init(token)
            return True, token
        except Exception as e:
            return False, str(e)

    def auth_totp(self, api_key: str, totp_secret: str) -> tuple[bool, str]:
        try:
            sec   = totp_secret.replace(" ", "").replace("-", "").upper()
            code  = pyotp.TOTP(sec).now()
            token = GrowwAPI.get_access_token(api_key=api_key, totp=code)
            self._init(token)
            return True, token
        except Exception as e:
            return False, str(e)

    def auth_key(self, api_key: str, secret: str) -> tuple[bool, str]:
        try:
            token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
            self._init(token)
            return True, token
        except Exception as e:
            return False, str(e)

    def disconnect(self) -> None:
        self._api = None
        self._ok  = False

    def _init(self, token: str) -> None:
        if not _SDK_AVAILABLE:
            raise RuntimeError("growwapi SDK not installed")
        self._api = GrowwAPI(token)
        self._ok  = True

    # ── Margin ────────────────────────────────────────────────────────────────

    def margin(self) -> dict:
        if not self._ok:
            return {"available": 0.0, "fno": 0.0, "mock": True}
        try:
            m   = self._api.get_available_margin_details()
            eq  = m.get("equity_margin_details", {})
            fno = m.get("fno_margin_details", {})
            return {
                "available": float(m.get("clear_cash", 0)),
                "equity":    float(eq.get("cnc_balance_available", 0)),
                "fno":       float(fno.get("future_balance_available", 0)),
                "mock":      False,
            }
        except Exception:
            return {"available": 0.0, "fno": 0.0, "mock": True}

    # ── Orders ────────────────────────────────────────────────────────────────

    def market_order(self, symbol: str, side: str, qty: int,
                     segment: str = "CASH", product: str = "MIS") -> dict:
        if not self._ok:
            return {"status": "SUCCESS", "order_id": "PAPER", "mock": True}
        try:
            seg  = self._seg(segment)
            prod = self._prod(product)
            txn  = self._api.TRANSACTION_TYPE_BUY if side == "BUY" \
                   else self._api.TRANSACTION_TYPE_SELL
            r    = self._api.place_order(
                trading_symbol=symbol, quantity=int(qty),
                validity=self._api.VALIDITY_DAY,
                exchange=self._api.EXCHANGE_NSE,
                segment=seg, product=prod,
                order_type=self._api.ORDER_TYPE_MARKET,
                transaction_type=txn,
            )
            return {"status": "SUCCESS", "order_id": r.get("groww_order_id", ""), "mock": False}
        except Exception as e:
            return {"status": "FAILED", "error": str(e), "mock": False}

    def get_ltp(self, *symbols: str, segment: str = "CASH") -> dict[str, float]:
        if not self._ok or not symbols:
            return {}
        try:
            seg   = self._seg(segment)
            syms  = tuple(f"NSE_{s}" for s in symbols)
            resp  = self._api.get_ltp(segment=seg, exchange_trading_symbols=syms[0] if len(syms) == 1 else syms)
            return {k.split("_", 1)[-1]: float(v) for k, v in resp.items()}
        except Exception:
            return {}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _seg(self, s: str):
        return self._api.SEGMENT_CASH if s.upper() in ("CASH", "EQUITY") \
               else self._api.SEGMENT_FNO

    def _prod(self, p: str):
        mapping = {
            "MIS": self._api.PRODUCT_MIS,
            "CNC": self._api.PRODUCT_CNC,
            "NRML": self._api.PRODUCT_NRML,
        }
        return mapping.get(p.upper(), self._api.PRODUCT_MIS)


# Module-level singleton
connector = GrowwConnector()
