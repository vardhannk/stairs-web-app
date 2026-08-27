"""
Comprehensive tests for models_v2.py — the rewritten trade handlers.

Categories:
  1. Options Buy signal handler (8 tests)
  2. Options AIT signal handler (8 tests)
  3. NiftyEXP signal + scheduler (6 tests)
  4. Expiry rollover (5 tests)
  5. Workstation handlers (3 tests)
  6. Edge cases — token, premium=0, orphan cleanup (5 tests)
  7. Quantity formulas (2 tests)
"""
import pytest
from unittest.mock import patch
from datetime import datetime
from conftest import (
    FakeApp, ist, monday_315pm, tuesday_315pm, midweek_noon, tuesday_noon
)


# Helpers ─────────────────────────────────────────────────────
def get_bundle(app, key):
    return app._kv.get(f"strategy_bundle::{key}", {"trades": [], "config": {}})


def open_trades(app, key):
    trades = get_bundle(app, key).get("trades", [])
    return [t for t in trades if t.get("status") == "OPEN"]


def closed_trades(app, key):
    trades = get_bundle(app, key).get("trades", [])
    return [t for t in trades if t.get("status") == "CLOSED"]


# ═══════════════════════════════════════════════════════════
# 1. OPTIONS BUY — directional buy
# ═══════════════════════════════════════════════════════════

class TestOptionsBuy:

    def test_no_position_long_signal_opens_long_ce(self, models_v2, app):
        models_v2._process_model_signal(app, "options_buy", "LONG", 23000, "2026-06-05T10:00:00Z")
        opens = open_trades(app, "options_buy")
        assert len(opens) == 1
        assert opens[0]["trend"] == "LONG"
        assert opens[0]["type"] == "CE"

    def test_no_position_short_signal_opens_short_pe(self, models_v2, app):
        models_v2._process_model_signal(app, "options_buy", "SHORT", 23000, "2026-06-05T10:00:00Z")
        opens = open_trades(app, "options_buy")
        assert len(opens) == 1
        assert opens[0]["trend"] == "SHORT"
        assert opens[0]["type"] == "PE"

    def test_open_long_same_long_signal_no_action(self, models_v2, app):
        models_v2._process_model_signal(app, "options_buy", "LONG", 23000, "t1")
        models_v2._process_model_signal(app, "options_buy", "LONG", 23050, "t2")
        opens = open_trades(app, "options_buy")
        assert len(opens) == 1
        assert opens[0]["entry_signal_time"] == "t1"  # not replaced

    def test_signal_flip_closes_old_opens_new(self, models_v2, app):
        models_v2._process_model_signal(app, "options_buy", "LONG", 23000, "t1")
        models_v2._process_model_signal(app, "options_buy", "SHORT", 22900, "t2")
        opens = open_trades(app, "options_buy")
        closes = closed_trades(app, "options_buy")
        assert len(opens) == 1
        assert len(closes) == 1
        assert closes[0]["trend"] == "LONG"
        assert opens[0]["trend"] == "SHORT"
        assert opens[0]["type"] == "PE"

    def test_two_orphan_opens_signal_flip_closes_both(self, models_v2, app):
        # Inject orphan state (simulates old duplicate-trade bug)
        app._kv["strategy_bundle::options_buy"] = {
            "trades": [
                {"date": "2026-06-04", "trend": "LONG", "status": "OPEN",
                 "entry_price": 100, "qty": 130, "expiry": "2026-06-09",
                 "tradingsymbol": "X", "spot": 23000, "type": "CE"},
                {"date": "2026-06-04", "trend": "LONG", "status": "OPEN",
                 "entry_price": 110, "qty": 130, "expiry": "2026-06-09",
                 "tradingsymbol": "Y", "spot": 23000, "type": "CE"},
            ],
            "config": {"capital": 1_000_000, "risk_per_trade": 0.07},
        }
        models_v2._process_model_signal(app, "options_buy", "SHORT", 22900, "t1")
        opens = open_trades(app, "options_buy")
        closes = closed_trades(app, "options_buy")
        assert len(opens) == 1, "Orphan cleanup must result in single open trade"
        assert len(closes) == 2, "Both orphans must be closed"
        assert opens[0]["trend"] == "SHORT"

    def test_token_expired_skips_write(self, models_v2, app):
        app.access_token_valid = False
        models_v2._process_model_signal(app, "options_buy", "LONG", 23000, "t1")
        opens = open_trades(app, "options_buy")
        assert len(opens) == 0, "Token expired = no trade written"
        # Must log warning
        warnings = [e for e in app.logged_events if e["level"] == "WARNING"]
        assert any("token missing" in w["message"].lower() for w in warnings)

    def test_zero_premium_skips_open(self, models_v2, app):
        app.directional_quote_responses[("LONG", "2026-06-09")] = {
            "tradingsymbol": "X", "option_type": "CE", "strike": 23000,
            "expiry": "2026-06-09", "premium": 0.0,
        }
        models_v2._process_model_signal(app, "options_buy", "LONG", 23000, "t1")
        opens = open_trades(app, "options_buy")
        assert len(opens) == 0, "Zero premium = no trade opened"
        warnings = [e for e in app.logged_events if e["level"] == "WARNING"]
        assert any("cannot open" in w["message"].lower() for w in warnings)

    def test_open_long_has_correct_fields(self, models_v2, app):
        models_v2._process_model_signal(app, "options_buy", "LONG", 23000, "sig_time_1")
        opens = open_trades(app, "options_buy")
        t = opens[0]
        assert t["status"] == "OPEN"
        assert t["trend"] == "LONG"
        assert t["spot"] == 23000.0
        assert t["expiry"] == "2026-06-09"
        assert t["entry_price"] == 100.0
        assert t["qty"] > 0
        assert t["qty"] % 65 == 0, "qty must be multiple of lot size"
        assert t["entry_signal_time"] == "sig_time_1"


# ═══════════════════════════════════════════════════════════
# 2. OPTIONS AIT — credit spread
# ═══════════════════════════════════════════════════════════

class TestOptionsAIT:

    def test_no_position_long_signal_opens_bull_put_spread(self, models_v2, app):
        models_v2._process_model_signal(app, "options_ait", "LONG", 23000, "t1")
        opens = open_trades(app, "options_ait")
        assert len(opens) == 1
        t = opens[0]
        assert t["type"] == "PE"
        assert t["atm_strike"] == 23000
        assert t["otm_strike"] == 22900  # 100 below for bull put
        assert t["atm_sell_price"] == 150.0
        assert t["otm_buy_price"] == 80.0

    def test_no_position_short_signal_opens_bear_call_spread(self, models_v2, app):
        models_v2._process_model_signal(app, "options_ait", "SHORT", 23000, "t1")
        opens = open_trades(app, "options_ait")
        assert len(opens) == 1
        t = opens[0]
        assert t["type"] == "CE"
        assert t["atm_strike"] == 23000
        assert t["otm_strike"] == 23100  # 100 above for bear call

    def test_signal_flip_closes_old_opens_new_spread(self, models_v2, app):
        models_v2._process_model_signal(app, "options_ait", "LONG", 23000, "t1")
        models_v2._process_model_signal(app, "options_ait", "SHORT", 22900, "t2")
        opens = open_trades(app, "options_ait")
        closes = closed_trades(app, "options_ait")
        assert len(opens) == 1
        assert len(closes) == 1
        assert closes[0]["trend"] == "LONG"
        assert opens[0]["trend"] == "SHORT"
        assert opens[0]["type"] == "CE"
        # Closed trade must have exit prices populated
        assert closes[0]["atm_exit_price"] != ""
        assert closes[0]["otm_exit_price"] != ""

    def test_same_signal_no_action(self, models_v2, app):
        models_v2._process_model_signal(app, "options_ait", "SHORT", 23000, "t1")
        models_v2._process_model_signal(app, "options_ait", "SHORT", 23050, "t2")
        opens = open_trades(app, "options_ait")
        assert len(opens) == 1

    def test_ait_qty_formula(self, models_v2, app):
        models_v2._process_model_signal(app, "options_ait", "LONG", 23000, "t1")
        opens = open_trades(app, "options_ait")
        # AIT default: ₹5,00,000 cap, 10% risk, 100 gap, 65 lot
        # qty = floor((500000 * 0.10) / 100 / 65) * 65 = floor(7.69) * 65 = 7 * 65 = 455
        assert opens[0]["qty"] == 455

    def test_orphan_cleanup_in_ait(self, models_v2, app):
        app._kv["strategy_bundle::options_ait"] = {
            "trades": [
                {"date": "2026-06-04", "trend": "LONG", "status": "OPEN",
                 "atm_sell_price": 150, "otm_buy_price": 80, "qty": 455,
                 "expiry": "2026-06-09", "atm_strike": 23000, "otm_strike": 22900,
                 "atm_tradingsymbol": "A", "otm_tradingsymbol": "B",
                 "spot": 23000, "type": "PE"},
                {"date": "2026-06-04", "trend": "LONG", "status": "OPEN",
                 "atm_sell_price": 160, "otm_buy_price": 90, "qty": 455,
                 "expiry": "2026-06-09", "atm_strike": 23000, "otm_strike": 22900,
                 "atm_tradingsymbol": "C", "otm_tradingsymbol": "D",
                 "spot": 23000, "type": "PE"},
            ],
            "config": {"capital": 500_000, "risk_factor": 0.10},
        }
        models_v2._process_model_signal(app, "options_ait", "SHORT", 22900, "t1")
        opens = open_trades(app, "options_ait")
        closes = closed_trades(app, "options_ait")
        assert len(opens) == 1
        assert len(closes) == 2
        assert opens[0]["trend"] == "SHORT"

    def test_ait_token_expired(self, models_v2, app):
        app.access_token_valid = False
        models_v2._process_model_signal(app, "options_ait", "LONG", 23000, "t1")
        assert len(open_trades(app, "options_ait")) == 0

    def test_ait_close_records_exit_prices(self, models_v2, app):
        models_v2._process_model_signal(app, "options_ait", "LONG", 23000, "t1")
        models_v2._process_model_signal(app, "options_ait", "SHORT", 22900, "t2")
        closes = closed_trades(app, "options_ait")
        assert "atm_exit_price" in closes[0]
        assert "otm_exit_price" in closes[0]
        assert closes[0]["exit_date"] != ""


# ═══════════════════════════════════════════════════════════
# 3. NIFTY EXP — time-windowed
# ═══════════════════════════════════════════════════════════

class TestNiftyExp:

    def test_midweek_signal_no_action(self, models_v2, app):
        # Patch datetime.now in models_v2's namespace where it gets imported
        import datetime as dt_module
        original_datetime = dt_module.datetime
        class FrozenDatetime(original_datetime):
            @classmethod
            def now(cls, tz=None):
                return midweek_noon() if tz else midweek_noon().replace(tzinfo=None)
        with patch.object(dt_module, "datetime", FrozenDatetime):
            models_v2.handle_nifty_exp_signal(app, "LONG", 23000, "t1")
        opens = open_trades(app, "nifty_exp")
        assert len(opens) == 0
        info = [e for e in app.logged_events if "outside trading window" in e["message"]]
        assert len(info) >= 1

    def test_tuesday_before_exit_window_signal_flip_works(self, models_v2, app):
        # Seed an OPEN LONG position
        app._kv["strategy_bundle::nifty_exp"] = {
            "trades": [{
                "date": "2026-06-08", "trend": "LONG", "status": "OPEN",
                "atm_sell_price": 150, "otm_buy_price": 80, "qty": 585,
                "expiry": "2026-06-09", "atm_strike": 23000, "otm_strike": 22900,
                "atm_tradingsymbol": "A", "otm_tradingsymbol": "B",
                "spot": 23000, "type": "PE",
            }],
            "config": {"capital": 500_000, "risk_factor": 0.12},
        }
        import datetime as dt_module
        original_datetime = dt_module.datetime
        class FrozenDatetime(original_datetime):
            @classmethod
            def now(cls, tz=None):
                return tuesday_noon() if tz else tuesday_noon().replace(tzinfo=None)
        with patch.object(dt_module, "datetime", FrozenDatetime):
            models_v2.handle_nifty_exp_signal(app, "SHORT", 22900, "t2")
        opens = open_trades(app, "nifty_exp")
        closes = closed_trades(app, "nifty_exp")
        assert len(opens) == 1
        assert len(closes) == 1
        assert opens[0]["trend"] == "SHORT"
        assert opens[0]["type"] == "CE"

    def test_monday_entry_uses_live_spot_and_prevailing_signal(self, models_v2, app):
        app.master_state["nifty_trend"] = "SHORT"
        app.live_spot_value = 23389.0
        models_v2.scheduler_nifty_exp_monday_entry(app)
        opens = open_trades(app, "nifty_exp")
        assert len(opens) == 1
        t = opens[0]
        assert t["trend"] == "SHORT"
        assert t["spot"] == 23389.0
        assert t["atm_strike"] == 23400  # round(23389, 100)
        assert t["otm_strike"] == 23500  # SHORT → ATM+100
        assert t["type"] == "CE"

    def test_monday_entry_idempotent(self, models_v2, app):
        app.master_state["nifty_trend"] = "LONG"
        models_v2.scheduler_nifty_exp_monday_entry(app)
        models_v2.scheduler_nifty_exp_monday_entry(app)
        opens = open_trades(app, "nifty_exp")
        assert len(opens) == 1, "Second call must be no-op (date marker)"

    def test_tuesday_exit_closes_all_no_rollover(self, models_v2, app):
        # Seed an OPEN trade
        app._kv["strategy_bundle::nifty_exp"] = {
            "trades": [{
                "date": "2026-06-08", "trend": "LONG", "status": "OPEN",
                "atm_sell_price": 150, "otm_buy_price": 80, "qty": 585,
                "expiry": "2026-06-09", "atm_strike": 23000, "otm_strike": 22900,
                "atm_tradingsymbol": "A", "otm_tradingsymbol": "B",
                "spot": 23000, "type": "PE",
            }],
            "config": {"capital": 500_000, "risk_factor": 0.12},
        }
        models_v2.scheduler_nifty_exp_tuesday_exit(app)
        opens = open_trades(app, "nifty_exp")
        closes = closed_trades(app, "nifty_exp")
        assert len(opens) == 0, "Tuesday exit must leave no open trades"
        assert len(closes) == 1
        # No rollover trade opened
        assert closes[0]["exit_reason"] == "tuesday_final_exit"

    def test_monday_entry_skipped_if_token_expired(self, models_v2, app):
        app.access_token_valid = False
        models_v2.scheduler_nifty_exp_monday_entry(app)
        assert len(open_trades(app, "nifty_exp")) == 0


# ═══════════════════════════════════════════════════════════
# 4. EXPIRY ROLLOVER (OB, AIT, OBW, AITW)
# ═══════════════════════════════════════════════════════════

class TestExpiryRollover:

    def test_rollover_closes_current_opens_next(self, models_v2, app):
        # Seed an OPEN trade with current expiry
        app._kv["strategy_bundle::options_buy"] = {
            "trades": [{
                "date": "2026-06-02", "trend": "LONG", "status": "OPEN",
                "entry_price": 100, "qty": 130, "expiry": "2026-06-09",
                "tradingsymbol": "X", "spot": 23000, "type": "CE",
            }],
            "config": {"capital": 1_000_000, "risk_per_trade": 0.07},
        }
        app.nearest_expiry = "2026-06-09"
        app.next_expiry = "2026-06-16"
        models_v2.scheduler_expiry_rollover(app, "options_buy")
        opens = open_trades(app, "options_buy")
        closes = closed_trades(app, "options_buy")
        assert len(opens) == 1
        assert len(closes) == 1
        assert opens[0]["expiry"] == "2026-06-16"
        assert opens[0]["trend"] == "LONG"  # Same direction
        assert closes[0]["exit_reason"] == "expiry_rollover"

    def test_rollover_idempotent_same_day(self, models_v2, app):
        app._kv["strategy_bundle::options_buy"] = {
            "trades": [{
                "date": "2026-06-02", "trend": "LONG", "status": "OPEN",
                "entry_price": 100, "qty": 130, "expiry": "2026-06-09",
                "tradingsymbol": "X", "spot": 23000, "type": "CE",
            }],
            "config": {"capital": 1_000_000, "risk_per_trade": 0.07},
        }
        models_v2.scheduler_expiry_rollover(app, "options_buy")
        first_count = len(get_bundle(app, "options_buy")["trades"])
        models_v2.scheduler_expiry_rollover(app, "options_buy")
        second_count = len(get_bundle(app, "options_buy")["trades"])
        assert second_count == first_count, "Second rollover on same day must be no-op"

    def test_rollover_with_no_open_trades(self, models_v2, app):
        # Empty bundle
        models_v2.scheduler_expiry_rollover(app, "options_buy")
        opens = open_trades(app, "options_buy")
        assert len(opens) == 0
        # Should log "no open trades to roll" — not crash
        info = [e for e in app.logged_events if "no open trades" in e["message"]]
        assert len(info) >= 1

    def test_rollover_ait_spread(self, models_v2, app):
        app._kv["strategy_bundle::options_ait"] = {
            "trades": [{
                "date": "2026-06-02", "trend": "SHORT", "status": "OPEN",
                "atm_sell_price": 150, "otm_buy_price": 80, "qty": 455,
                "expiry": "2026-06-09", "atm_strike": 23000, "otm_strike": 23100,
                "atm_tradingsymbol": "A", "otm_tradingsymbol": "B",
                "spot": 23000, "type": "CE",
            }],
            "config": {"capital": 500_000, "risk_factor": 0.10},
        }
        models_v2.scheduler_expiry_rollover(app, "options_ait")
        opens = open_trades(app, "options_ait")
        assert len(opens) == 1
        assert opens[0]["expiry"] == "2026-06-16"
        assert opens[0]["trend"] == "SHORT"  # Direction preserved
        assert opens[0]["type"] == "CE"

    def test_rollover_token_expired_skips(self, models_v2, app):
        app._kv["strategy_bundle::options_buy"] = {
            "trades": [{
                "date": "2026-06-02", "trend": "LONG", "status": "OPEN",
                "entry_price": 100, "qty": 130, "expiry": "2026-06-09",
                "tradingsymbol": "X", "spot": 23000, "type": "CE",
            }],
            "config": {"capital": 1_000_000, "risk_per_trade": 0.07},
        }
        app.access_token_valid = False
        models_v2.scheduler_expiry_rollover(app, "options_buy")
        # No close, no new open — entirely skipped
        trades = get_bundle(app, "options_buy")["trades"]
        assert all(t["status"] == "OPEN" for t in trades), "Token expired = no state change"


# ═══════════════════════════════════════════════════════════
# 5. WORKSTATION HANDLERS
# ═══════════════════════════════════════════════════════════

class TestWorkstation:

    def test_workstation_signal_routes_to_both_obw_and_aitw(self, models_v2, app):
        models_v2.handle_workstation_signal(app, "LONG", 23000, "t1")
        assert len(open_trades(app, "ob_workstation")) == 1
        assert len(open_trades(app, "ait_workstation")) == 1

    def test_workstation_independent_from_main(self, models_v2, app):
        # Open main OB trade
        models_v2._process_model_signal(app, "options_buy", "LONG", 23000, "t1")
        # Workstation signal SHORT shouldn't affect main OB
        models_v2.handle_workstation_signal(app, "SHORT", 22900, "t2")
        main_opens = open_trades(app, "options_buy")
        assert len(main_opens) == 1
        assert main_opens[0]["trend"] == "LONG"  # Unchanged
        # But workstation should be SHORT
        ws_opens = open_trades(app, "ob_workstation")
        assert ws_opens[0]["trend"] == "SHORT"

    def test_workstation_storage_keys_separate(self, models_v2, app):
        models_v2._process_model_signal(app, "ob_workstation", "LONG", 23000, "t1")
        assert "strategy_bundle::ob_workstation" in app._kv
        assert "strategy_bundle::options_buy" not in app._kv or \
               len(app._kv.get("strategy_bundle::options_buy", {}).get("trades", [])) == 0


# ═══════════════════════════════════════════════════════════
# 6. EDGE CASES
# ═══════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_find_open_trade_empty_list(self, models_v2):
        idx, trade = models_v2.find_open_trade([])
        assert idx is None and trade is None

    def test_find_all_open_trades_mixed_status(self, models_v2):
        trades = [
            {"status": "CLOSED"},
            {"status": "OPEN", "id": 1},
            {"status": "CLOSED"},
            {"status": "OPEN", "id": 2},
        ]
        opens = models_v2.find_all_open_trades(trades)
        assert len(opens) == 2
        assert opens[0][0] == 1 and opens[1][0] == 3

    def test_round_to_100(self, models_v2):
        # round_to_100 rounds to nearest 100
        assert models_v2.round_to_100(23389) == 23400
        assert models_v2.round_to_100(23449) == 23400
        assert models_v2.round_to_100(23451) == 23500
        assert models_v2.round_to_100(23000) == 23000
        # Note: exactly halfway (23450) uses banker's rounding — implementation detail

    def test_handle_main_signal_routes_to_all_three(self, models_v2, app):
        # Patch datetime so NiftyEXP no-action path runs cleanly (mid-week)
        import datetime as dt_module
        original_datetime = dt_module.datetime
        class FrozenDatetime(original_datetime):
            @classmethod
            def now(cls, tz=None):
                return midweek_noon() if tz else midweek_noon().replace(tzinfo=None)
        with patch.object(dt_module, "datetime", FrozenDatetime):
            models_v2.handle_main_signal(app, "LONG", 23000, "t1")
        assert len(open_trades(app, "options_buy")) == 1
        assert len(open_trades(app, "options_ait")) == 1
        assert len(open_trades(app, "nifty_exp")) == 0  # No action mid-week

    def test_scheduler_tick_outside_windows_does_nothing(self, models_v2, app):
        # Random mid-week time
        models_v2.scheduler_tick(app, midweek_noon())
        assert len(open_trades(app, "options_buy")) == 0
        assert len(open_trades(app, "nifty_exp")) == 0


# ═══════════════════════════════════════════════════════════
# 7. QUANTITY FORMULAS
# ═══════════════════════════════════════════════════════════

class TestQuantityFormulas:

    def test_directional_qty_formula(self, models_v2):
        # OB: capital=10L, risk=7%, premium=100 → floor(70000/100/65)*65 = floor(10.77)*65 = 650
        qty = models_v2.compute_qty_directional(1_000_000, 0.07, 100.0, 65)
        assert qty == 650

    def test_spread_qty_formula(self, models_v2):
        # AIT: capital=5L, risk=10% → floor(50000/100/65)*65 = floor(7.69)*65 = 455
        qty = models_v2.compute_qty_spread(500_000, 0.10, 65)
        assert qty == 455

    def test_nifty_exp_qty_formula(self, models_v2):
        # NE: capital=5L, risk=12% → floor(60000/100/65)*65 = floor(9.23)*65 = 585
        qty = models_v2.compute_qty_spread(500_000, 0.12, 65)
        assert qty == 585

    def test_zero_premium_returns_zero_qty(self, models_v2):
        qty = models_v2.compute_qty_directional(1_000_000, 0.07, 0.0, 65)
        assert qty == 0
