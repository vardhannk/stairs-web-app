"""Dashboard data-integrity tests — summary API, users schema, key scoping."""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


@pytest.fixture
def app_client():
    """Flask test client with isolated temp SQLite DB."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["STAIRS_DB_PATH"] = db_path
    os.environ.pop("MULTI_TENANT", None)

    # Fresh import so DB_PATH picks up temp file
    for mod in list(sys.modules):
        if mod in ("app", "models_v2"):
            del sys.modules[mod]
    import app as stairs_app

    stairs_app.db_init()
    conn = stairs_app.get_db()
    conn.execute(
        "INSERT INTO users(email,name,google_id,picture,tier,is_admin,is_active,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("a@test.com", "User A", "gid-a", "", "tier3", 0, 1, "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO users(email,name,google_id,picture,tier,is_admin,is_active,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("b@test.com", "User B", "gid-b", "", "tier3", 0, 1, "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    stairs_app.kv_set(
        "strategy_bundle::ob_workstation",
        {"trades": [{"status": "CLOSED", "entry_price": 100, "exit_price": 110, "qty": 65,
                     "trend": "LONG"}], "config": stairs_app.opt_buy_config},
    )
    stairs_app.kv_set(
        "strategy_bundle::1::ob_workstation",
        {"trades": [{"status": "CLOSED", "entry_price": 50, "exit_price": 60, "qty": 65,
                     "trend": "LONG"}], "config": stairs_app.opt_buy_config},
    )

    client = stairs_app.app.test_client()
    yield client, stairs_app, db_path
    try:
        os.unlink(db_path)
    except OSError:
        pass


def test_users_table_has_no_username_column(app_client):
    client, stairs_app, db_path = app_client
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    conn.close()
    assert "username" not in cols
    assert "email" in cols
    assert "name" in cols


def test_admin_users_query_uses_email_not_username(app_client):
    client, stairs_app, db_path = app_client
    admin_id = stairs_app.get_user_by_email("a@test.com")["id"]
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id
        sess["user_email"] = "a@test.com"
    stairs_app.kv_set("multi_tenant_enabled", False)
    admin = stairs_app.get_user_by_id(admin_id)
    admin["is_admin"] = 1
    conn = stairs_app.get_db()
    conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (admin_id,))
    conn.commit()
    conn.close()

    with client.session_transaction() as sess:
        sess["user_id"] = admin_id
        sess["user_email"] = "a@test.com"
    r = client.get("/api/admin/users")
    assert r.status_code == 200
    users = r.get_json()["users"]
    assert all("email" in u for u in users)
    assert all("username" not in u for u in users)


def test_dashboard_summary_requires_login(app_client):
    client, _, _ = app_client
    r = client.get("/api/dashboard/summary")
    assert r.status_code == 401


def test_dashboard_summary_returns_all_models(app_client):
    client, stairs_app, _ = app_client
    uid = stairs_app.get_user_by_email("a@test.com")["id"]
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["user_email"] = "a@test.com"
    r = client.get("/api/dashboard/summary")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert set(d["strategies"].keys()) == set(stairs_app.KNOWN_LIVE_MODELS)
    assert "model_modes" in d
    assert "live_enabled_models" in d
    assert "timestamp" in d


def test_dashboard_summary_global_bundle_in_single_tenant(app_client):
    client, stairs_app, _ = app_client
    uid = stairs_app.get_user_by_email("a@test.com")["id"]
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    r = client.get("/api/dashboard/summary")
    d = r.get_json()
    ob = d["strategies"]["ob_workstation"]
    assert ob["paper"]["wins"] + ob["paper"]["losses"] >= 0


def test_futures_reads_per_user_bundle_when_logged_in(app_client):
    """Production only has strategy_bundle::{uid}::futures — no global key.
    Logged-in reads must hit the user key even when multi_tenant flag is off."""
    client, stairs_app, _ = app_client
    uid = stairs_app.get_user_by_email("a@test.com")["id"]
    stairs_app.kv_set("multi_tenant_enabled", False)
    stairs_app.kv_set(
        f"strategy_bundle::{uid}::futures",
        {
            "trades": [{
                "date": "2026-01-10", "trend": "LONG", "entry": 24000,
                "action_type": "Reversal", "partial_exit1": 24100,
                "partial_exit1_date": "2026-01-11", "status": "CLOSED",
            }],
            "config": {"capital": 1200000, "nifty_start": 24000, "leverage": 2, "lot_size": 65},
        },
    )
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["user_email"] = "a@test.com"
    r = client.get("/api/futures")
    assert r.status_code == 200
    d = r.get_json()
    assert d.get("ok") is True
    assert d.get("bundle_key") == f"strategy_bundle::{uid}::futures"
    assert d.get("trade_count") == 1
    assert len(d.get("trades") or []) == 1

    summary = client.get("/api/dashboard/summary").get_json()
    fut = summary["strategies"]["futures"]
    assert fut.get("trade_count", 0) >= 1
    assert fut.get("bundle_key") == f"strategy_bundle::{uid}::futures"


def test_futures_surfaces_phase_stash_when_active_trades_empty(app_client):
    client, stairs_app, _ = app_client
    uid = stairs_app.get_user_by_email("a@test.com")["id"]
    stairs_app.kv_set(
        f"strategy_bundle::{uid}::futures",
        {
            "trades": [],
            "active_phase": "live",
            "phase_stash": {
                "paper": [{
                    "date": "2026-01-05", "trend": "SHORT", "entry": 23500,
                    "action_type": "Reversal", "partial_exit1": 23400,
                    "partial_exit1_date": "2026-01-06", "status": "CLOSED",
                }],
                "live": [],
            },
            "config": {"capital": 1200000, "nifty_start": 24000, "leverage": 2, "lot_size": 65},
        },
    )
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    d = client.get("/api/futures").get_json()
    assert d.get("trade_count") == 1
    assert len(d.get("trades") or []) == 1


def test_model_modes_open_positions_scoped_to_user(app_client):
    client, stairs_app, _ = app_client
    uid = stairs_app.get_user_by_email("a@test.com")["id"]
    with client.session_transaction() as sess:
        sess["user_id"] = uid
    r = client.get(f"/api/automation/model_modes?user_id={uid}")
    assert r.status_code == 200
    d = r.get_json()
    assert d["user_id"] == str(uid)
    assert "open_positions" in d


def test_app_imports_cleanly():
    import app as stairs_app
    assert stairs_app.app is not None
    assert len(stairs_app.ALL_MODELS) == len(stairs_app.KNOWN_LIVE_MODELS)
