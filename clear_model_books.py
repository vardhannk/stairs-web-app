#!/usr/bin/env python3
"""Wipe Futures / OB / AIT paper+live books. Keeps Nifty EXP.

  cd /opt/stairs-web-app
  sudo systemctl stop stairs-web-app   # optional but safest
  sudo ./venv/bin/python clear_model_books.py --yes
  sudo systemctl start stairs-web-app
"""
from __future__ import annotations
import argparse
import os
import sys

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually wipe (required)")
    args = ap.parse_args()
    os.chdir(os.path.dirname(os.path.abspath(__file__)) or ".")
    import app as stairs  # noqa: E402

    targets = list(stairs.WIPEABLE_MODEL_BOOKS)
    print("Will WIPE paper+live for:", ", ".join(targets))
    print("Will KEEP: nexp_workstation, nifty_strangle_w")
    if not args.yes:
        print("Dry run only. Re-run with --yes to wipe.")
        return 0
    result = stairs.wipe_model_books(targets, include_instances=True)
    import json
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1

if __name__ == "__main__":
    sys.exit(main())
