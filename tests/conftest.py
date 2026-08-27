"""
pytest fixtures for models_v2 tests.

Provides:
  - mock_app: a fake app module that models_v2 expects
  - fresh_state: an in-memory kv store (mimics SQLite kv table)
  - frozen_time: lets tests control current IST time
"""
import sys
import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

# Make /opt/stairs-web-app importable so we can import models_v2
APP_DIR = os.environ.get("STAIRS_APP_DIR", "/opt/stairs-web-app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


IST = timezone(timedelta(hours=5, minutes=30))


class FakeApp:
    """Stand-in for the app.py module that models_v2 imports functions from."""

    def __init__(self):
        # In-memory kv store
        self._kv = {}
        # Mock master state — handlers may read nifty_trend, nifty_spot, nifty_lot_size
        self.master_state = {
            "nifty_trend": "LONG",
            "nifty_spot": 23000.0,
            "nifty_lot_size": 65,
        }
        self.CURRENT_NIFTY_LOT_SIZE = 65
        # Logged events for assertions
        self.logged_events = []
        # Telegram sent messages
        self.telegram_sent = []
        # Token gate — set False to simulate expired token
        self.access_token_valid = True
        # Mock quote responses
        self.directional_quote_responses = {}  # (signal, expiry) -> dict
        self.spread_quote_responses = {}       # (signal, expiry) -> dict
        # Live spot mock
        self.live_spot_value = 23000.0
        # Expiry mocks
        self.nearest_expiry = "2026-06-09"
        self.next_expiry = "2026-06-16"

    # ── kv store interface ─────────────────────────────────────
    def kv_get(self, key, default=None):
        if key not in self._kv:
            return default if default is not None else {}
        return self._kv[key]

    def kv_set(self, key, value):
        self._kv[key] = value

    # ── logging / notifications ─────────────────────────────────
    def log_automation(self, message, level="INFO", details=None):
        self.logged_events.append({"level": level, "message": message, "details": details})

    def send_telegram(self, message):
        self.telegram_sent.append(message)

    # ── Zerodha mock ────────────────────────────────────────────
    def get_access_token(self):
        return "fake_token" if self.access_token_valid else None

    def get_kite(self, require_token=False):
        m = MagicMock()
        m.ltp.return_value = {"NSE:NIFTY 50": {"last_price": self.live_spot_value}}
        return m

    # ── Expiry helpers ──────────────────────────────────────────
    def get_nearest_nifty_weekly_expiry(self):
        return self.nearest_expiry

    def get_next_nifty_weekly_expiry_after(self, expiry):
        return self.next_expiry

    # ── Quote helpers ───────────────────────────────────────────
    def quote_option(self, signal, spot, expiry):
        key = (signal, expiry)
        if key in self.directional_quote_responses:
            return self.directional_quote_responses[key]
        # Default — synthesize a quote
        atm = int(round(spot / 100.0) * 100)
        opt_type = "CE" if signal == "LONG" else "PE"
        return {
            "tradingsymbol": f"NIFTY{expiry[2:].replace('-','')}{atm}{opt_type}",
            "option_type":   opt_type,
            "strike":        atm,
            "expiry":        expiry,
            "premium":       100.0,
        }

    def get_nifty_spread_quote(self, signal, spot, expiry, gap):
        key = (signal, expiry)
        if key in self.spread_quote_responses:
            return self.spread_quote_responses[key]
        atm = int(round(spot / 100.0) * 100)
        otm = atm - gap if signal == "LONG" else atm + gap
        opt_type = "PE" if signal == "LONG" else "CE"
        return {
            "atm_strike":        atm,
            "otm_strike":        otm,
            "option_type":       opt_type,
            "atm_tradingsymbol": f"NIFTY26JUN{atm}{opt_type}",
            "otm_tradingsymbol": f"NIFTY26JUN{otm}{opt_type}",
            "atm_sell_premium":  150.0,
            "otm_buy_premium":   80.0,
            "expiry":            expiry,
        }

    def now_utc_iso(self):
        return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def app():
    """Fresh FakeApp per test."""
    return FakeApp()


@pytest.fixture
def models_v2():
    """Fresh import of models_v2 module."""
    if "models_v2" in sys.modules:
        del sys.modules["models_v2"]
    import models_v2 as m
    return m


# ── Time helpers ──────────────────────────────────────────────
def ist(year, month, day, hour, minute):
    """Helper to build IST datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=IST)


def monday_315pm():
    return ist(2026, 6, 8, 15, 15)   # Monday 8 June 2026 at 15:15 IST


def tuesday_315pm():
    return ist(2026, 6, 9, 15, 15)   # Tuesday 9 June 2026 at 15:15 IST


def midweek_noon():
    return ist(2026, 6, 10, 12, 0)   # Wednesday 12 PM IST


def tuesday_noon():
    return ist(2026, 6, 9, 12, 0)    # Tuesday before exit window
