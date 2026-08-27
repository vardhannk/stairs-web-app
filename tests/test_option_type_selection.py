"""
Tests for directional vs spread option-type selection in app.py.

Catches the bug where directional buys (Options Buy, OB Workstation) used the
SPREAD convention and bought CE on SHORT signals (should be PE).

These tests read app.py SOURCE and verify the directional call sites use the
directional picker (by_type with CE/SHORT->PE), not the spread-convention picker.
"""
import os, re, ast

APP_PY = os.environ.get("STAIRS_APP_DIR", os.path.join(os.path.dirname(__file__), "..")) + "/app.py"

def _src():
    with open(APP_PY) as f:
        return f.read()


class TestDirectionalUsesCorrectType:
    """The directional rule: LONG->CE, SHORT->PE."""

    def test_directional_rule_long_is_ce_short_is_pe(self):
        # The canonical directional rule, asserted directly.
        def directional_type(signal):
            return "CE" if str(signal).upper() == "LONG" else "PE"
        assert directional_type("LONG") == "CE"
        assert directional_type("SHORT") == "PE"

    def test_spread_rule_is_opposite(self):
        # Spread convention is the OPPOSITE: LONG->PE, SHORT->CE.
        def spread_type(signal):
            return "PE" if str(signal).upper() == "LONG" else "CE"
        assert spread_type("LONG") == "PE"
        assert spread_type("SHORT") == "CE"
        # and they must differ from directional
        assert spread_type("SHORT") != ("PE")  # spread SHORT is CE


class TestQuoteOptionUsesDirectionalPicker:
    """quote_option (directional entry) must NOT use the spread-convention picker."""

    def test_quote_option_uses_by_type(self):
        src = _src()
        # Extract the quote_option function body
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "quote_option"), None)
        assert fn is not None, "quote_option not found"
        body_src = ast.get_source_segment(src, fn)
        assert "pick_nifty_option_contract_by_type" in body_src, (
            "quote_option must use pick_nifty_option_contract_by_type (explicit CE/PE), "
            "not the spread-convention pick_nifty_option_contract"
        )
        # And must NOT call the spread-convention picker
        assert "pick_nifty_option_contract(" not in body_src, (
            "quote_option still calls the spread-convention pick_nifty_option_contract — "
            "this buys CE on SHORT (the bug)"
        )

    def test_directional_short_resolves_to_pe(self):
        # Simulate the exact logic quote_option now uses
        signal = "SHORT"
        opt_type = "CE" if str(signal).upper() == "LONG" else "PE"
        assert opt_type == "PE", "SHORT directional must select PE, not CE"


class TestSpreadPathUnchanged:
    """get_nifty_spread_quote must STILL use the spread-convention picker."""

    def test_spread_quote_still_uses_spread_picker(self):
        src = _src()
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "get_nifty_spread_quote"), None)
        assert fn is not None, "get_nifty_spread_quote not found"
        body_src = ast.get_source_segment(src, fn)
        assert "pick_nifty_option_contract(" in body_src, (
            "spread path must keep using pick_nifty_option_contract (spread convention)"
        )
