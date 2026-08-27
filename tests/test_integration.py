"""
Integration tests for STAIRS app.py <-> models_v2.py wiring.

These tests catch real-world failure modes that unit tests miss:
  - "models_v2 not defined" at runtime (today's bug)
  - Webhook code path doesn't actually invoke models_v2
  - Production app.py has dead/wrong call sites
  - Service startup fails due to import errors
  - Scheduler isn't wired to models_v2.scheduler_tick

Strategy:
  We don't import all of app.py (it has heavy external deps).
  Instead we parse the source to verify the wiring contract:
    - Required imports are present
    - Function calls to models_v2 are at expected locations
    - Old handlers are commented out (no double-invocation)
  And we execute a minimal smoke test that proves models_v2 is reachable.
"""
import os
import re
import sys
import importlib.util
import pytest


APP_DIR = os.environ.get("STAIRS_APP_DIR", "/opt/stairs-web-app")
APP_PY = os.path.join(APP_DIR, "app.py")
MODELS_V2_PY = os.path.join(APP_DIR, "models_v2.py")


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def read_app_source():
    """Read the current app.py source — the FILE on disk, not cached."""
    with open(APP_PY, "r") as f:
        return f.read()


def read_models_v2_source():
    with open(MODELS_V2_PY, "r") as f:
        return f.read()


def import_models_v2():
    """Import models_v2.py FRESH each call."""
    if "models_v2" in sys.modules:
        del sys.modules["models_v2"]
    spec = importlib.util.spec_from_file_location("models_v2", MODELS_V2_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ═══════════════════════════════════════════════════════════════
# 1. STARTUP & IMPORT VERIFICATION
# These would have caught today's bug.
# ═══════════════════════════════════════════════════════════════

class TestStartup:

    def test_app_py_file_exists(self):
        """app.py must exist on disk at the expected path."""
        assert os.path.exists(APP_PY), f"app.py missing at {APP_PY}"

    def test_models_v2_file_exists(self):
        """models_v2.py must exist on disk."""
        assert os.path.exists(MODELS_V2_PY), f"models_v2.py missing at {MODELS_V2_PY}"

    def test_app_py_has_models_v2_import(self):
        """
        CRITICAL: This is the test that would have caught today's bug.
        app.py must import models_v2 — otherwise webhook fails at runtime
        with 'name models_v2 is not defined'.
        """
        src = read_app_source()
        assert re.search(r"^\s*import\s+models_v2\b", src, re.MULTILINE), \
            "app.py is MISSING 'import models_v2' — webhook will crash at runtime!"

    def test_models_v2_imports_cleanly(self):
        """models_v2.py loads without syntax or import errors."""
        try:
            m = import_models_v2()
            assert m is not None
        except Exception as e:
            pytest.fail(f"models_v2.py failed to import: {e}")

    def test_models_v2_has_required_public_functions(self):
        """All public entry points must exist with the right names."""
        m = import_models_v2()
        required = [
            "handle_main_signal",
            "handle_workstation_signal",
            "scheduler_tick",
            "handle_nifty_exp_signal",
            "scheduler_nifty_exp_monday_entry",
            "scheduler_nifty_exp_tuesday_exit",
            "scheduler_expiry_rollover",
            "fetch_live_nifty_spot",
            "find_open_trade",
            "find_all_open_trades",
            "compute_qty_directional",
            "compute_qty_spread",
            "round_to_100",
        ]
        missing = [name for name in required if not hasattr(m, name)]
        assert not missing, f"models_v2 missing functions: {missing}"


# ═══════════════════════════════════════════════════════════════
# 2. WIRING VERIFICATION
# Ensures app.py actually calls models_v2 at the right places.
# ═══════════════════════════════════════════════════════════════

class TestWiring:

    def test_main_webhook_calls_models_v2_handle_main_signal(self):
        """
        Main TradingView webhook must call models_v2.handle_main_signal.
        Without this, OB / AIT / NiftyEXP get no signals.
        """
        src = read_app_source()
        # Find the webhook function definition
        webhook_match = re.search(
            r"def\s+api_tradingview_webhook\s*\(",
            src
        )
        assert webhook_match, "api_tradingview_webhook function not found in app.py"
        # The call must appear AFTER this point
        rest = src[webhook_match.start():]
        # End at the next top-level def or route
        next_route = re.search(r"\n@app\.route|\ndef\s+(?!api_tradingview_webhook)", rest[10:])
        webhook_block = rest[:next_route.start() + 10] if next_route else rest
        assert "models_v2.handle_main_signal" in webhook_block, \
            "Main webhook does NOT call models_v2.handle_main_signal — OB/AIT/NiftyEXP will not respond to signals!"

    def test_workstation_webhook_calls_models_v2(self):
        """Workstation webhook must call models_v2.handle_workstation_signal."""
        src = read_app_source()
        ws_match = re.search(
            r"def\s+api_tradingview_workstation_webhook\s*\(",
            src
        )
        if not ws_match:
            pytest.skip("Workstation webhook not present in this build")
        rest = src[ws_match.start():]
        next_route = re.search(r"\n@app\.route|\ndef\s+", rest[10:])
        ws_block = rest[:next_route.start() + 10] if next_route else rest
        assert "models_v2.handle_workstation_signal" in ws_block, \
            "Workstation webhook does NOT call models_v2.handle_workstation_signal!"

    def test_scheduler_calls_models_v2_scheduler_tick(self):
        """position_sync_job scheduler must invoke models_v2.scheduler_tick every cycle."""
        src = read_app_source()
        sched_match = re.search(r"def\s+position_sync_job\s*\(", src)
        assert sched_match, "position_sync_job function not found"
        rest = src[sched_match.start():]
        # Search a generous window
        sched_block = rest[:30000]
        assert "models_v2.scheduler_tick" in sched_block, \
            "Scheduler does NOT call models_v2.scheduler_tick — Monday entry / Tuesday rollover will NOT fire!"

    def test_no_duplicate_handler_invocation(self):
        """
        Ensure the OLD signal handlers (handle_ob_on_signal etc.) are NOT being
        called from direct_module_store_sync_patch — otherwise we get duplicate trades.
        """
        src = read_app_source()
        dms_match = re.search(r"def\s+direct_module_store_sync_patch\s*\(", src)
        if not dms_match:
            pytest.skip("direct_module_store_sync_patch not present")
        rest = src[dms_match.start():]
        # Find the body up to the next def
        next_def = re.search(r"\ndef\s+", rest[20:])
        block = rest[:next_def.start() + 20] if next_def else rest

        # Old handler calls must be commented out or removed
        # Look for an ACTIVE (non-commented) line calling old handlers
        for old_handler in [
            "handle_ob_on_signal",
            "handle_ait_on_signal",
            "handle_nifty_exp_on_signal",
        ]:
            for line in block.split("\n"):
                stripped = line.strip()
                # Skip if commented out
                if stripped.startswith("#"):
                    continue
                # Skip if it's just a definition not a call
                if stripped.startswith("def "):
                    continue
                # If the line contains the old handler call (with paren), that's a duplicate-invocation bug
                if re.search(rf"\b{old_handler}\s*\(", stripped):
                    pytest.fail(
                        f"DUPLICATE INVOCATION RISK: direct_module_store_sync_patch "
                        f"still calls {old_handler} — this causes duplicate trades. "
                        f"Line: {stripped}"
                    )


# ═══════════════════════════════════════════════════════════════
# 3. END-TO-END SMOKE TESTS
# Actually invoke models_v2 functions like the webhook would.
# ═══════════════════════════════════════════════════════════════

class FakeAppModule:
    """Minimal stand-in for the app module — only what models_v2 touches."""
    def __init__(self):
        self._kv = {}
        self.master_state = {
            "nifty_trend": "LONG",
            "nifty_spot": 23000.0,
            "nifty_lot_size": 65,
        }
        self.CURRENT_NIFTY_LOT_SIZE = 65
        self.logs = []
        self.telegrams = []

    def kv_get(self, key, default=None):
        return self._kv.get(key, default if default is not None else {})

    def kv_set(self, key, value):
        self._kv[key] = value

    def log_automation(self, message, level="INFO", details=None):
        self.logs.append({"level": level, "message": message})

    def send_telegram(self, message):
        self.telegrams.append(message)

    def get_access_token(self):
        return "fake_token"

    def get_kite(self, require_token=False):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.ltp.return_value = {"NSE:NIFTY 50": {"last_price": 23000.0}}
        return m

    def get_nearest_nifty_weekly_expiry(self):
        return "2026-06-09"

    def get_next_nifty_weekly_expiry_after(self, expiry):
        return "2026-06-16"

    def quote_option(self, signal, spot, expiry):
        atm = int(round(spot / 100.0) * 100)
        opt_type = "CE" if signal == "LONG" else "PE"
        return {
            "tradingsymbol": f"NIFTY26JUN{atm}{opt_type}",
            "option_type": opt_type, "strike": atm,
            "expiry": expiry, "premium": 100.0,
        }

    def get_nifty_spread_quote(self, signal, spot, expiry, gap):
        atm = int(round(spot / 100.0) * 100)
        otm = atm - gap if signal == "LONG" else atm + gap
        opt_type = "PE" if signal == "LONG" else "CE"
        return {
            "atm_strike": atm, "otm_strike": otm, "option_type": opt_type,
            "atm_tradingsymbol": f"NIFTY26JUN{atm}{opt_type}",
            "otm_tradingsymbol": f"NIFTY26JUN{otm}{opt_type}",
            "atm_sell_premium": 150.0, "otm_buy_premium": 80.0,
            "expiry": expiry,
        }

    def now_utc_iso(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


class TestEndToEnd:
    """Smoke tests: invoke models_v2 exactly the way app.py's webhook does."""

    def test_main_signal_e2e_creates_trades(self):
        """Simulate: main webhook receives a LONG signal -> all 3 modules get a trade."""
        m = import_models_v2()
        app = FakeAppModule()

        # This is what app.py's webhook does:
        m.handle_main_signal(app, "LONG", 23000.0, "2026-06-05T10:00:00Z")

        ob = app._kv.get("strategy_bundle::options_buy", {}).get("trades", [])
        ait = app._kv.get("strategy_bundle::options_ait", {}).get("trades", [])

        assert len(ob) == 1 and ob[0]["status"] == "OPEN", \
            "OB did not get a trade after main signal"
        assert len(ait) == 1 and ait[0]["status"] == "OPEN", \
            "AIT did not get a trade after main signal"

    def test_main_signal_flip_e2e(self):
        """Simulate two consecutive signals (LONG then SHORT) — verify flip behavior."""
        m = import_models_v2()
        app = FakeAppModule()

        m.handle_main_signal(app, "LONG", 23000.0, "t1")
        m.handle_main_signal(app, "SHORT", 22900.0, "t2")

        ob = app._kv.get("strategy_bundle::options_buy", {}).get("trades", [])
        opens = [t for t in ob if t["status"] == "OPEN"]
        closes = [t for t in ob if t["status"] == "CLOSED"]

        assert len(opens) == 1, f"After flip, expected 1 OPEN, got {len(opens)}"
        assert len(closes) == 1, f"After flip, expected 1 CLOSED, got {len(closes)}"
        assert opens[0]["trend"] == "SHORT"
        assert closes[0]["trend"] == "LONG"

    def test_workstation_signal_e2e(self):
        """Workstation webhook -> OBW + AITW trades created independently."""
        m = import_models_v2()
        app = FakeAppModule()

        m.handle_workstation_signal(app, "LONG", 23000.0, "t1")

        obw = app._kv.get("strategy_bundle::ob_workstation", {}).get("trades", [])
        aitw = app._kv.get("strategy_bundle::ait_workstation", {}).get("trades", [])

        assert len(obw) == 1 and obw[0]["status"] == "OPEN"
        assert len(aitw) == 1 and aitw[0]["status"] == "OPEN"

        # Main strategy should NOT be affected
        ob = app._kv.get("strategy_bundle::options_buy", {}).get("trades", [])
        assert len(ob) == 0, "Workstation signal accidentally affected main OB!"

    def test_scheduler_tick_outside_window_is_noop(self):
        """Scheduler tick at mid-week noon -> no trades created (correct behavior)."""
        m = import_models_v2()
        app = FakeAppModule()
        from datetime import datetime, timezone, timedelta

        IST = timezone(timedelta(hours=5, minutes=30))
        wed_noon = datetime(2026, 6, 10, 12, 0, tzinfo=IST)

        m.scheduler_tick(app, wed_noon)

        # No trades should be created mid-week
        assert all(
            len(app._kv.get(f"strategy_bundle::{n}", {}).get("trades", [])) == 0
            for n in ["options_buy", "options_ait", "nifty_exp", "ob_workstation", "ait_workstation"]
        ), "Scheduler tick outside window created unexpected trades!"

    def test_scheduler_tick_monday_315_fires_nifty_exp_entry(self):
        """Scheduler tick on Monday 3:15 PM IST -> NiftyEXP Monday entry fires."""
        m = import_models_v2()
        app = FakeAppModule()
        from datetime import datetime, timezone, timedelta

        IST = timezone(timedelta(hours=5, minutes=30))
        mon_315 = datetime(2026, 6, 8, 15, 15, tzinfo=IST)

        m.scheduler_tick(app, mon_315)

        ne = app._kv.get("strategy_bundle::nifty_exp", {}).get("trades", [])
        assert len(ne) == 1, "NiftyEXP Monday entry didn't fire!"
        assert ne[0]["status"] == "OPEN"


# ═══════════════════════════════════════════════════════════════
# 4. CONTRACT TESTS
# Verify the interface contract between app.py and models_v2.
# ═══════════════════════════════════════════════════════════════

class TestContract:

    def test_models_v2_uses_app_module_pattern(self):
        """
        models_v2 must take an `app` (module) parameter — not access globals.
        This is what allows the FakeAppModule pattern to work for tests.
        """
        m = import_models_v2()
        import inspect

        sig = inspect.signature(m.handle_main_signal)
        params = list(sig.parameters.keys())
        assert params[0] == "app", \
            f"handle_main_signal must take 'app' as first param, got: {params}"

        sig2 = inspect.signature(m.handle_workstation_signal)
        params2 = list(sig2.parameters.keys())
        assert params2[0] == "app"

        sig3 = inspect.signature(m.scheduler_tick)
        params3 = list(sig3.parameters.keys())
        assert params3[0] == "app"

    def test_models_v2_does_not_reference_old_handler_names(self):
        """
        models_v2.py must not call the old handler names — they're dead code in app.py
        and any reference would be a sign of bad refactoring.
        """
        src = read_models_v2_source()
        bad_refs = [
            "handle_ob_on_signal",
            "handle_ait_on_signal",
            "handle_nifty_exp_on_signal",
            "handle_ob_expiry_rollover",
            "handle_ait_expiry_rollover",
            "handle_obw_on_signal",
            "handle_aitw_on_signal",
        ]
        found = [name for name in bad_refs if name in src]
        assert not found, f"models_v2 references old handler names: {found}"
