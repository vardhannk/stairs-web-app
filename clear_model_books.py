#!/usr/bin/env python3
"""Wipe Futures / OB / AIT paper+live books. Keeps Nifty EXP.

Does NOT import Flask/app.py (avoids authlib / venv issues). Talks to SQLite kv.

  cd /opt/stairs-web-app
  sudo systemctl stop stairs-web-app
  sudo python3 clear_model_books.py --yes
  sudo systemctl start stairs-web-app
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

MODELS = ("futures", "ob_workstation", "ait_workstation")
KEEP = ("nexp_workstation", "nifty_strangle_w")


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def find_db(root: str) -> str:
    env = os.environ.get("STAIRS_DB_PATH")
    if env and os.path.isfile(env):
        return env
    for name in ("stairs_state.db", "stairs.db", "app.db"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    # newest *.db in root
    dbs = [
        os.path.join(root, f)
        for f in os.listdir(root)
        if f.endswith(".db") and os.path.isfile(os.path.join(root, f))
    ]
    if not dbs:
        raise SystemExit(f"No SQLite db found under {root}")
    dbs.sort(key=os.path.getmtime, reverse=True)
    return dbs[0]


def kv_get(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return row[0]


def kv_set(conn: sqlite3.Connection, key: str, value) -> None:
    payload = json.dumps(value)
    conn.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, payload, now_iso()),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually wipe (required)")
    ap.add_argument("--db", default="", help="path to stairs sqlite db")
    args = ap.parse_args()

    root = os.path.dirname(os.path.abspath(__file__)) or "."
    db_path = args.db or find_db(root)
    print(f"DB: {db_path}")
    print("Will WIPE paper+live for:", ", ".join(MODELS))
    print("Will KEEP:", ", ".join(KEEP))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Discover keys
    rows = conn.execute("SELECT key FROM kv").fetchall()
    all_keys = [r["key"] for r in rows]

    def is_target_bundle(key: str) -> bool:
        if not key.startswith("strategy_bundle::"):
            return False
        parts = key.split("::")
        model = parts[-1]
        if model in KEEP:
            return False
        # strategy_bundle::<model>
        if len(parts) == 2 and parts[1] in MODELS:
            return True
        # strategy_bundle::<uid>::<model>
        if len(parts) == 3 and parts[1] != "inst" and parts[2] in MODELS:
            return True
        return False

    target_keys = [k for k in all_keys if is_target_bundle(k)]

    # Strategy instances
    strategies = kv_get(conn, "strategies") or []
    if isinstance(strategies, list):
        for st in strategies:
            if not isinstance(st, dict):
                continue
            if st.get("type") in MODELS and st.get("id"):
                k = "strategy_bundle::inst::%s" % st["id"]
                if k not in target_keys:
                    target_keys.append(k)

    print(f"Bundles to wipe: {len(target_keys)}")
    for k in sorted(target_keys):
        print(" ", k)

    if not args.yes:
        print("\nDry run only. Re-run with --yes to wipe.")
        conn.close()
        return 0

    wiped = {}
    for key in target_keys:
        data = kv_get(conn, key)
        if not isinstance(data, dict):
            data = {}
        n_trades = len(data.get("trades") or [])
        n_arch = len(data.get("archive") or [])
        stash = data.get("phase_stash") or {}
        n_paper = len(stash.get("paper") or []) if isinstance(stash, dict) else 0
        n_live = len(stash.get("live") or []) if isinstance(stash, dict) else 0
        n_seed = len(data.get("seed_backup") or [])
        cfg = dict(data.get("config") or {}) if isinstance(data.get("config"), dict) else {}
        cfg.pop("signal_time", None)
        cfg.pop("signal_source", None)
        data["trades"] = []
        data["archive"] = []
        data["phase_stash"] = {"paper": [], "live": []}
        data.pop("seed_backup", None)
        data["active_phase"] = "paper"
        data["config"] = cfg
        kv_set(conn, key, data)
        wiped[key] = {
            "trades": n_trades,
            "archive": n_arch,
            "phase_paper": n_paper,
            "phase_live": n_live,
            "seed_backup": n_seed,
        }

    # Position caches
    drop_names = set(MODELS) | {"options_buy", "options_ait"}
    pos_keys = [k for k in all_keys if k == "dry_run_module_positions"
                or k == "workstation_positions"
                or k.startswith("dry_run_module_positions::")
                or k.startswith("workstation_positions::")]
    for pk in pos_keys:
        pos = kv_get(conn, pk)
        if not isinstance(pos, dict) or not pos:
            continue
        before = len(pos)
        for name in list(pos.keys()):
            if name in drop_names or any(str(name).startswith(m) for m in MODELS):
                pos.pop(name, None)
        if len(pos) != before:
            kv_set(conn, pk, pos)
            wiped[pk] = {"removed_keys": before - len(pos)}

    auto = kv_get(conn, "automation_state")
    if isinstance(auto, dict) and auto.get("active_position"):
        auto = dict(auto)
        auto["active_position"] = None
        kv_set(conn, "automation_state", auto)
        wiped["automation_state.active_position"] = {"cleared": True}

    conn.commit()
    conn.close()

    # Sanity: NEXP keys still present with data untouched (just report counts)
    print("\nNEXP left intact (sample):")
    conn2 = sqlite3.connect(db_path)
    for key in sorted(k for k in all_keys if "nexp_workstation" in k):
        data = kv_get(conn2, key)
        if isinstance(data, dict):
            print(f"  {key}: trades={len(data.get('trades') or [])} archive={len(data.get('archive') or [])}")
    conn2.close()

    print("\nWiped:")
    print(json.dumps({"ok": True, "models": list(MODELS), "wiped": wiped}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
