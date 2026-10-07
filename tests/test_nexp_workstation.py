"""
Tests for the NiftyEXP Workstation model (nexp_workstation).

Verifies: config exists with correct params, the direction helper reads the
prevailing OB/AIT workstation direction, and the schedulers are wired into
scheduler_tick.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import models_v2


class TestNexpWorkstationConfig:
    def test_config_exists(self):
        assert "nexp_workstation" in models_v2.MODEL_CONFIG
        cfg = models_v2.MODEL_CONFIG["nexp_workstation"]
        assert cfg["type"] == "credit_spread"
        assert cfg["storage"] == "strategy_bundle::nexp_workstation"
        assert cfg["channel"] == "workstation"
        assert cfg.get("windowed") is True
        assert cfg.get("no_rollover") is True

    def test_config_params(self):
        cfg = models_v2.MODEL_CONFIG["nexp_workstation"]
        assert cfg["capital"] == 500_000
        assert abs(cfg["risk_pct"] - 0.12) < 1e-9


class _FakeApp:
    def __init__(self, store):
        self.store = store
    def kv_get(self, key, default=None):
        return self.store.get(key, default if default is not None else {})


class TestPrevailingDirection:
    def test_reads_open_obw_direction(self):
        store = {
            "strategy_bundle::ob_workstation": {
                "trades": [
                    {"trend": "LONG", "status": "CLOSED"},
                    {"trend": "SHORT", "status": "OPEN"},
                ]
            }
        }
        app = _FakeApp(store)
        assert models_v2._prevailing_workstation_direction(app) == "SHORT"

    def test_falls_back_to_last_closed(self):
        store = {
            "strategy_bundle::ob_workstation": {
                "trades": [{"trend": "LONG", "status": "CLOSED"}]
            },
            "strategy_bundle::ait_workstation": {"trades": []},
        }
        app = _FakeApp(store)
        assert models_v2._prevailing_workstation_direction(app) == "LONG"

    def test_none_when_no_data(self):
        app = _FakeApp({})
        assert models_v2._prevailing_workstation_direction(app) is None

    def _tv_app(self, store, **ms):
        app = _FakeApp(store)
        app.master_state = ms
        return app

    def _ago(self, days):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def test_falls_back_to_recent_tradingview_signal(self):
        app = self._tv_app({}, signal_source="TRADINGVIEW", nifty_trend="SHORT", signal_received_at=self._ago(2))
        assert models_v2._prevailing_workstation_direction(app) == "SHORT"

    def test_workstation_trade_beats_tradingview_signal(self):
        store = {"strategy_bundle::ob_workstation": {"trades": [{"trend": "LONG", "status": "OPEN"}]}}
        app = self._tv_app(store, signal_source="TRADINGVIEW", nifty_trend="SHORT", signal_received_at=self._ago(1))
        assert models_v2._prevailing_workstation_direction(app) == "LONG"

    def test_ignores_stale_tradingview_signal(self):
        app = self._tv_app({}, signal_source="TRADINGVIEW", nifty_trend="LONG", signal_received_at=self._ago(20))
        assert models_v2._prevailing_workstation_direction(app) is None

    def test_ignores_signal_without_received_time(self):
        app = self._tv_app({}, signal_source="TRADINGVIEW", nifty_trend="LONG", signal_time="2026-09-23T14:51:56Z")
        assert models_v2._prevailing_workstation_direction(app) is None

    def test_ignores_manual_signal(self):
        app = self._tv_app({}, signal_source="MANUAL", nifty_trend="LONG", signal_received_at=self._ago(1))
        assert models_v2._prevailing_workstation_direction(app) is None


class TestSchedulersExist:
    def test_monday_and_tuesday_schedulers_defined(self):
        assert hasattr(models_v2, "scheduler_nexp_workstation_monday_entry")
        assert hasattr(models_v2, "scheduler_nexp_workstation_tuesday_exit")

    def test_wired_into_scheduler_tick(self):
        import inspect
        src = inspect.getsource(models_v2.scheduler_tick)
        assert "scheduler_nexp_workstation_monday_entry(app)" in src
        assert "scheduler_nexp_workstation_tuesday_exit(app)" in src
