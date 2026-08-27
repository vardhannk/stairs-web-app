"""
Reproduction tests for the OB Workstation / Options Buy contract bugs.

Run against CURRENT models_v2 -> these FAIL (proving they catch the bug).
Run against FIXED models_v2   -> these PASS.

The Jun 12 -> Jun 16 scenario:
  - Open LONG at spot 23378  -> should buy 23400 CE
  - Rollover-close at spot 23988 -> must still price the 23400 CE (the held contract)
  - With the bug, close re-derives 24000 CE and prices that instead.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import models_v2
from faithful_fakeapp import FaithfulFakeApp


def _seed_ob_workstation_open_long(app):
    """Simulate the Jun 12 LONG entry at spot 23378 the way the handler does."""
    app.nearest_expiry = "2026-06-16"
    # Price book: the 23400 CE was ~126 at entry on Jun 12
    app.price_book["NIFTY26JUN1623400CE"] = 126.0
    # set up empty bundle
    app.kv["strategy_bundle::ob_workstation"] = {"trades": [], "config": {"capital": 1_000_000, "risk_per_trade": 0.07}}
    app.kv["workstation_positions"] = {}
    # Process the open via the real handler
    models_v2._process_model_signal(app, "ob_workstation", "LONG", 23378.05, "2026-06-12T05:45:00Z")


class TestDirectionalEntryRecordsContract:
    """Bug 1: build_directional_trade must store tradingsymbol + type."""

    def test_entry_records_tradingsymbol(self):
        app = FaithfulFakeApp()
        _seed_ob_workstation_open_long(app)
        trades = app.kv["strategy_bundle::ob_workstation"]["trades"]
        assert len(trades) == 1, f"expected 1 trade, got {len(trades)}"
        t = trades[0]
        assert t["tradingsymbol"], (
            f"BUG 1: tradingsymbol is empty/null ({t.get('tradingsymbol')!r}). "
            f"build_directional_trade read it from the wrong place in the quote dict."
        )
        assert "23400CE" in t["tradingsymbol"], (
            f"expected 23400 CE, got {t['tradingsymbol']}"
        )

    def test_entry_records_type(self):
        app = FaithfulFakeApp()
        _seed_ob_workstation_open_long(app)
        t = app.kv["strategy_bundle::ob_workstation"]["trades"][0]
        assert t["type"] == "CE", f"BUG 1: type should be CE, got {t.get('type')!r}"

    def test_entry_price_correct(self):
        app = FaithfulFakeApp()
        _seed_ob_workstation_open_long(app)
        t = app.kv["strategy_bundle::ob_workstation"]["trades"][0]
        assert abs(t["entry_price"] - 126.0) < 0.01, (
            f"entry price should be 126.0 (the 23400 CE premium), got {t['entry_price']}"
        )


class TestDirectionalCloseUsesStoredContract:
    """Bug 3: close must price the HELD contract, not re-derive from close-time spot."""

    def test_rollover_close_prices_held_contract(self):
        app = FaithfulFakeApp()
        _seed_ob_workstation_open_long(app)

        # Now it's Jun 16 rollover. Spot has moved to 23988.
        # The held 23400 CE is now worth ~589 (deep ITM).
        # A (wrong) re-derived 24000 CE would be worth ~9 (near worthless).
        app.master_state["nifty_spot"] = 23988.0
        app.price_book["NIFTY26JUN1623400CE"] = 589.0   # held contract, real value
        app.price_book["NIFTY26JUN1624000CE"] = 9.55    # wrong re-derived contract
        app.nearest_expiry = "2026-06-16"
        app.next_expiry = "2026-06-23"
        # new rollover entry contract
        app.price_book["NIFTY26JUN2324000CE"] = 175.0

        models_v2.scheduler_expiry_rollover(app, "ob_workstation")

        trades = app.kv["strategy_bundle::ob_workstation"]["trades"]
        closed = trades[0]
        assert closed["status"] == "CLOSED"
        exit_price = float(closed["exit_price"])
        assert abs(exit_price - 589.0) < 0.01, (
            f"BUG 3: rollover close priced the WRONG contract. "
            f"Exit price={exit_price} but the held 23400 CE was worth 589.0. "
            f"The close path re-derived a 24000 CE (worth 9.55) from close-time spot."
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
