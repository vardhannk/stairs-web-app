"""
stairs_store.py
===============
Thin helpers to read/write momentum state into the EXISTING STAIRS SQLite
database (kv + automation_log tables). No schema changes required — we live
under namespaced keys so we never touch your trade models.

Keys used:
    momentum:latest      -> JSON: {date, target:{sym:wt}, regime, equity_snap}
    momentum:history     -> JSON list of past targets (capped)
    momentum:status      -> JSON: {last_run, ok, message}
"""
from __future__ import annotations
import json, os, sqlite3, datetime as dt
from contextlib import contextmanager

# Default path; override at runtime with env STAIRS_DB_PATH or by passing db_path.
DB_PATH = os.environ.get("STAIRS_DB_PATH", "/opt/stairs-web-app/stairs_state.db")

LATEST_KEY = "momentum:latest"
HISTORY_KEY = "momentum:history"
STATUS_KEY = "momentum:status"
HISTORY_CAP = 60


def _resolve(db_path):
    return db_path or os.environ.get("STAIRS_DB_PATH") or DB_PATH


@contextmanager
def _conn(db_path=None):
    con = sqlite3.connect(_resolve(db_path), timeout=30)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _ensure_kv(con):
    # kv already exists in STAIRS; create-if-missing keeps this self-contained
    con.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")


def kv_get(key: str, db_path=None):
    with _conn(db_path) as con:
        _ensure_kv(con)
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def kv_set(key: str, value, db_path=None):
    with _conn(db_path) as con:
        _ensure_kv(con)
        con.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=str)),
        )


def log_event(event: str, detail: str = "", db_path=None):
    """Append to automation_log if present; fall back to a kv log."""
    ts = dt.datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as con:
        try:
            con.execute(
                "INSERT INTO automation_log(timestamp, event, detail) VALUES(?,?,?)",
                (ts, f"momentum:{event}", detail),
            )
        except sqlite3.OperationalError:
            # automation_log schema differs or missing -> kv fallback
            _ensure_kv(con)
            row = con.execute("SELECT value FROM kv WHERE key=?",
                              ("momentum:log",)).fetchone()
            logs = json.loads(row[0]) if row else []
            logs.append({"ts": ts, "event": event, "detail": detail})
            logs = logs[-200:]
            con.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("momentum:log", json.dumps(logs)),
            )


def save_target(date, target: dict, regime: bool, equity_snap=None,
                db_path=None):
    payload = {
        "date": str(date),
        "regime_invested": bool(regime),
        "target": {k: round(float(v), 4) for k, v in target.items()},
        "cash": round(1.0 - sum(target.values()), 4),
        "equity_snap": equity_snap,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    kv_set(LATEST_KEY, payload, db_path)
    hist = kv_get(HISTORY_KEY, db_path) or []
    hist.append(payload)
    kv_set(HISTORY_KEY, hist[-HISTORY_CAP:], db_path)
    return payload


def set_status(ok: bool, message: str, db_path=None):
    kv_set(STATUS_KEY, {
        "last_run": dt.datetime.now().isoformat(timespec="seconds"),
        "ok": bool(ok), "message": message,
    }, db_path)
