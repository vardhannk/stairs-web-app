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


class TestSchedulersExist:
    def test_monday_and_tuesday_schedulers_defined(self):
        assert hasattr(models_v2, "scheduler_nexp_workstation_monday_entry")
        assert hasattr(models_v2, "scheduler_nexp_workstation_tuesday_exit")

    def test_wired_into_scheduler_tick(self):
        import inspect
        src = inspect.getsource(models_v2.scheduler_tick)
        assert "scheduler_nexp_workstation_monday_entry(app)" in src
        assert "scheduler_nexp_workstation_tuesday_exit(app)" in src
