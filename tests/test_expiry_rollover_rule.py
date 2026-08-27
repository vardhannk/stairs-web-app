"""
Tests for the primer rollover rule on new entries:
"next expiry only at/after 3:15 PM IST on expiry day".

Reproduces the July 7 bug: a signal at 15:15:07 IST on expiry day must open on
the NEXT expiry, not the same-day (near-worthless) expiry.
"""
import os, sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(__file__))
import models_v2

IST = timezone(timedelta(hours=5, minutes=30))

class _App:
    def get_next_nifty_weekly_expiry_after(self, expiry):
        # July 7 2026 (Tue) -> July 14 2026 (next Tue)
        return "2026-07-14"

def test_expiry_day_at_315_uses_next():
    app = _App()
    now = datetime(2026, 7, 7, 15, 15, 7, tzinfo=IST)  # the exact July-7 bug moment
    out = models_v2._entry_expiry_with_rollover_rule(app, "2026-07-07", now)
    assert out == "2026-07-14", f"expected next expiry, got {out}"

def test_expiry_day_after_315_uses_next():
    app = _App()
    now = datetime(2026, 7, 7, 15, 20, 0, tzinfo=IST)
    assert models_v2._entry_expiry_with_rollover_rule(app, "2026-07-07", now) == "2026-07-14"

def test_expiry_day_before_315_uses_same():
    app = _App()
    now = datetime(2026, 7, 7, 11, 0, 0, tzinfo=IST)  # morning of expiry day
    assert models_v2._entry_expiry_with_rollover_rule(app, "2026-07-07", now) == "2026-07-07"

def test_expiry_day_exactly_314_uses_same():
    app = _App()
    now = datetime(2026, 7, 7, 15, 14, 59, tzinfo=IST)  # one second before window
    assert models_v2._entry_expiry_with_rollover_rule(app, "2026-07-07", now) == "2026-07-07"

def test_non_expiry_day_uses_same():
    app = _App()
    now = datetime(2026, 7, 6, 15, 30, 0, tzinfo=IST)  # Monday, nearest expiry is Tue 7th
    assert models_v2._entry_expiry_with_rollover_rule(app, "2026-07-07", now) == "2026-07-07"

def test_non_expiry_day_even_after_315_uses_same():
    app = _App()
    now = datetime(2026, 7, 3, 16, 0, 0, tzinfo=IST)  # Friday afternoon
    assert models_v2._entry_expiry_with_rollover_rule(app, "2026-07-07", now) == "2026-07-07"
