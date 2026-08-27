"""
Behavior-faithful FakeApp for models_v2 testing.

KEY DIFFERENCE from the old conftest mock:
  - quote_option returns the SAME nested shape as production app.py:
        {'contract': {'tradingsymbol':..., 'option_type':...}, 'premium':..., ...}
  - The tradingsymbol ENCODES the strike, so passing the wrong spot at close
    time produces a detectably-wrong contract.
  - get_nifty_spread_quote returns the same top-level shape as production.

This is what makes the contract-drift bug catchable: the fake models the real
behaviour that "the contract you get depends on the spot you pass in".
"""
import math


def _round_to_100(v):
    return int(round(float(v) / 100.0) * 100)


def _expiry_compact(expiry_iso):
    # "2026-06-16" -> "26JUN16" style tag (good enough to make symbols unique)
    y, m, d = expiry_iso.split("-")
    months = ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    return f"{y[2:]}{months[int(m)]}{d}"


class FaithfulFakeApp:
    """Mirrors the subset of app.py that models_v2 calls, with REAL data shapes."""

    def __init__(self):
        self.logs = []
        self.telegrams = []
        self.kv = {}
        self._token = True
        # Price book: {tradingsymbol: premium}. Tests set this to control quotes.
        # If a symbol isn't in here, premium defaults to a strike-derived synthetic value.
        self.price_book = {}
        self.nearest_expiry = "2026-06-16"
        self.next_expiry = "2026-06-23"
        self.master_state = {"nifty_lot_size": 65}

    # ---- logging / telegram ----
    def log_automation(self, msg, level="INFO", details=None):
        self.logs.append((level, msg))

    def send_telegram(self, msg):
        self.telegrams.append(msg)

    # ---- kv store ----
    def kv_get(self, key, default=None):
        return self.kv.get(key, default if default is not None else {})

    def kv_set(self, key, value):
        self.kv[key] = value

    # ---- token ----
    def get_access_token(self):
        return self._token

    # ---- time/expiry ----
    def now_utc_iso(self):
        return "2026-06-16T09:44:00Z"

    def get_nearest_nifty_weekly_expiry(self):
        return self.nearest_expiry

    def get_next_nifty_weekly_expiry_after(self, cur):
        return self.next_expiry

    # ---- spot ----
    def fetch_live_nifty_spot(self):
        return 23988.0

    # ---- THE CRITICAL ONE: quote_option (directional) ----
    # Mirrors production: returns nested 'contract', strike derived from spot.
    def quote_option(self, signal, spot, expiry=None):
        if not expiry:
            expiry = self.nearest_expiry
        strike = _round_to_100(spot)
        opt_type = "CE" if str(signal).upper() == "LONG" else "PE"
        tag = _expiry_compact(expiry)
        tradingsymbol = f"NIFTY{tag}{strike}{opt_type}"
        premium = self.price_book.get(tradingsymbol)
        if premium is None:
            # synthetic fallback so unset symbols still return *something*
            premium = max(1.0, abs(spot - strike) + 50.0)
        contract = {
            "exchange": "NFO",
            "tradingsymbol": tradingsymbol,
            "instrument_token": 99999,
            "strike": strike,
            "option_type": opt_type,
            "expiry": expiry,
        }
        return {
            "contract": contract,
            "instrument_key": f"NFO:{tradingsymbol}",
            "premium": float(premium),
            "quote_timestamp": "2026-06-16T09:44:00Z",
        }

    # ---- quote by exact symbol (the fix relies on this existing) ----
    def quote_option_by_symbol(self, tradingsymbol):
        premium = self.price_book.get(tradingsymbol)
        if premium is None:
            premium = 1.0  # unknown symbol -> near-zero (mirrors expired OTM)
        return {
            "contract": {"tradingsymbol": tradingsymbol, "exchange": "NFO"},
            "instrument_key": f"NFO:{tradingsymbol}",
            "premium": float(premium),
            "quote_timestamp": "2026-06-16T09:44:00Z",
        }

    # ---- spread quote (credit spread) ----
    def get_nifty_spread_quote(self, signal, spot, expiry=None, strike_gap=100):
        if not expiry:
            expiry = self.nearest_expiry
        atm = _round_to_100(spot)
        otm = atm - strike_gap if str(signal).upper() == "LONG" else atm + strike_gap
        opt_type = "PE" if str(signal).upper() == "LONG" else "CE"
        tag = _expiry_compact(expiry)
        atm_sym = f"NIFTY{tag}{atm}{opt_type}"
        otm_sym = f"NIFTY{tag}{otm}{opt_type}"
        return {
            "signal": signal, "spot": float(spot), "expiry": expiry,
            "atm_strike": atm, "otm_strike": int(otm), "option_type": opt_type,
            "atm_tradingsymbol": atm_sym, "otm_tradingsymbol": otm_sym,
            "atm_sell_premium": float(self.price_book.get(atm_sym, 100.0)),
            "otm_buy_premium": float(self.price_book.get(otm_sym, 40.0)),
            "quote_time": "2026-06-16T09:44:00Z",
        }

    # ---- spread quote by explicit symbols (the fix relies on this) ----
    def get_spread_quote_by_symbols(self, atm_tradingsymbol, otm_tradingsymbol):
        return {
            "atm_tradingsymbol": atm_tradingsymbol,
            "otm_tradingsymbol": otm_tradingsymbol,
            "atm_sell_premium": float(self.price_book.get(atm_tradingsymbol, 0.05)),
            "otm_buy_premium": float(self.price_book.get(otm_tradingsymbol, 0.05)),
        }


class _FakeKite:
    def __init__(self, app):
        self.app = app
    def ltp(self, keys):
        return {"NSE:NIFTY 50": {"last_price": self.app.master_state.get("nifty_spot", 23988.0)}}


def _get_kite(self, require_token=True):
    return _FakeKite(self)

FaithfulFakeApp.get_kite = _get_kite
