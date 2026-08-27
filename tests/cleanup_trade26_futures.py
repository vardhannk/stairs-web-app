#!/usr/bin/env python3
"""
Cleanup trade [26] in futures bundle.

What's wrong with [26]:
  - SHORT trade opened 11-Jun at 14:15 IST, entry=23205.8
  - LONG signal arrived 12-Jun at 09:15 IST → trade [26] should have CLOSED
  - But sync_positions_with_zerodha ran first (during a brief DRY_RUN→LIVE
    window) and marked [26] CLOSED with exit_price="manual" placeholder
  - exit_signal_time is missing

This script fixes:
  - exit_price: "manual" → 23206 (NIFTY26JUNFUT actual close price)
  - exit_signal_time: missing → "2026-06-12T03:45:00Z" (09:15 IST today, matching trade [27]'s entry signal time)
  - Adds exit_reason: "signal_flip_via_sync" for clarity
  - Preserves existing exit_date

Safety:
  - Reads only trade [26]
  - Refuses to run if trade [26] doesn't match expected pre-state
  - Prints before/after diff
"""
import sqlite3
import json
import sys

DB = "/opt/stairs-web-app/stairs_state.db"

EXPECTED_TRADE_INDEX = 26
EXPECTED_DATE = "2026-06-11"
EXPECTED_TREND = "SHORT"
EXPECTED_ENTRY = 23205.8

NEW_EXIT_PRICE = 23206.0
NEW_EXIT_SIGNAL_TIME = "2026-06-12T03:45:00Z"
NEW_EXIT_REASON = "signal_flip_via_sync"

conn = sqlite3.connect(DB)
row = conn.execute("SELECT value FROM kv WHERE key='strategy_bundle::futures'").fetchone()
if not row:
    print("ERROR: futures bundle not found in kv")
    sys.exit(1)

data = json.loads(row[0])
trades = data.get("trades", [])

if len(trades) <= EXPECTED_TRADE_INDEX:
    print(f"ERROR: trade [{EXPECTED_TRADE_INDEX}] does not exist (futures has only {len(trades)} trades)")
    sys.exit(1)

t = trades[EXPECTED_TRADE_INDEX]
print("=== BEFORE ===")
print(json.dumps(t, indent=2))

# Sanity checks — refuse to update if this isn't the trade we expect
checks = [
    ("date", t.get("date"), EXPECTED_DATE),
    ("trend", t.get("trend"), EXPECTED_TREND),
    ("entry", t.get("entry"), EXPECTED_ENTRY),
    ("status", t.get("status"), "CLOSED"),
]
mismatches = [(field, got, want) for (field, got, want) in checks if got != want]
if mismatches:
    print("\nERROR: trade [26] does not match expected pre-state. Aborting.")
    for field, got, want in mismatches:
        print(f"  {field}: got={got!r} expected={want!r}")
    sys.exit(1)

# Apply updates
t["exit_price"] = NEW_EXIT_PRICE
t["exit_signal_time"] = NEW_EXIT_SIGNAL_TIME
t["exit_reason"] = NEW_EXIT_REASON

trades[EXPECTED_TRADE_INDEX] = t
data["trades"] = trades

conn.execute(
    "UPDATE kv SET value=? WHERE key='strategy_bundle::futures'",
    (json.dumps(data),),
)
conn.commit()
conn.close()

print("\n=== AFTER ===")
print(json.dumps(t, indent=2))
print("\n✅ Trade [26] updated. Service restart NOT needed (DB-only change).")
