"""Kite data + order plumbing shared by the VolCluster and GC Options Buy models.

Both models run on ONE account: the instance token in single-account mode, or
the first admin's own token when multi-tenant is on. Orders always go through
app.place_live_order_with_retry, so the per-model live gate still decides
paper vs live.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
NIFTY_TOKEN = 256265
VIX_TOKEN = 264969
NIFTY_STRIKE_STEP = 50

_bars_cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
_bars_lock = threading.Lock()


def A():
    """The running app module (loaded as `app` under gunicorn)."""
    return sys.modules.get("app") or sys.modules["__main__"]


def owner_user_id():
    """None in single-account mode; the first admin's id when multi-tenant."""
    a = A()
    if not a.multi_tenant_enabled():
        return None
    try:
        admins = [u for u in a.get_all_users() if u.get("is_admin")]
        if admins:
            return min(int(u["id"]) for u in admins)
    except Exception:
        pass
    return None


def kite():
    return A().get_kite(require_token=True, user_id=owner_user_id())


def connected() -> bool:
    try:
        return bool(A().get_access_token(owner_user_id()))
    except Exception:
        return False


def model_mode(model: str) -> str:
    return A().get_model_mode(model, owner_user_id())


def kill_switch_on() -> bool:
    try:
        return bool(A().automation_state.get("kill_switch"))
    except Exception:
        return False


def log(message: str, level: str = "INFO", details: dict | None = None) -> None:
    try:
        A().log_automation(message, level=level, details=details)
    except Exception:
        pass


def index_bars(token: int, interval: str, days: int, max_age_s: float = 50.0) -> pd.DataFrame:
    """Kite historical candles, lowercase OHLCV, naive IST index (bar open time)."""
    key = (token, interval, days)
    now = time.time()
    with _bars_lock:
        hit = _bars_cache.get(key)
        if hit and now - hit[0] < max_age_s:
            return hit[1]
    to_dt = datetime.now(IST).replace(tzinfo=None)
    rows = kite().historical_data(token, to_dt - timedelta(days=days), to_dt, interval)
    df = pd.DataFrame(rows or [])
    if df.empty:
        return df
    idx = pd.to_datetime(df.pop("date"))
    if idx.dt.tz is not None:
        idx = idx.dt.tz_convert(IST).dt.tz_localize(None)
    df.index = pd.DatetimeIndex(idx)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    with _bars_lock:
        _bars_cache[key] = (now, df)
    return df


def ltp(keys: list[str]) -> dict[str, float]:
    q = kite().ltp(keys) or {}
    return {k: float((q.get(k) or {}).get("last_price") or 0) for k in keys}


def nifty_spot() -> float:
    try:
        return ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]
    except Exception:
        return 0.0


def india_vix(default: float = 16.0) -> float:
    try:
        v = ltp(["NSE:INDIA VIX"])["NSE:INDIA VIX"]
        if v > 0:
            return v
    except Exception:
        pass
    return default


def futures_contract(skip_near_expiry: bool = False) -> dict[str, Any]:
    """Nearest NIFTY future; with skip_near_expiry, next month inside 7 days of expiry."""
    a = A()
    prev = getattr(a._kite_ctx, "user_id", None)
    a._kite_ctx.user_id = owner_user_id()
    try:
        return a.pick_nifty_futures_contract(use_rollover_logic=skip_near_expiry)
    finally:
        a._kite_ctx.user_id = prev


def nearest_option_expiry() -> str:
    a = A()
    prev = getattr(a._kite_ctx, "user_id", None)
    a._kite_ctx.user_id = owner_user_id()
    try:
        return a.get_nearest_nifty_weekly_expiry()
    finally:
        a._kite_ctx.user_id = prev


def option_contract(opt_type: str, strike: float, expiry: str) -> dict[str, Any]:
    a = A()
    prev = getattr(a._kite_ctx, "user_id", None)
    a._kite_ctx.user_id = owner_user_id()
    try:
        return a.pick_nifty_option_contract_at_strike(opt_type.upper(), int(strike), expiry[:10])
    finally:
        a._kite_ctx.user_id = prev


def nfo_ltp(tradingsymbol: str) -> float:
    if not tradingsymbol:
        return 0.0
    key = f"NFO:{tradingsymbol}"
    try:
        return ltp([key])[key]
    except Exception:
        return 0.0


def place(model: str, txn: str, tradingsymbol: str, qty: int, mark: float, tag: str) -> dict[str, Any]:
    """One NFO market order through the app's live gate. Never retried: a failed
    order is reported and the next scheduler pass re-evaluates from scratch."""
    a = A()
    uid = owner_user_id()
    if model_mode(model) == "live" and kill_switch_on():
        return {"ok": False, "message": "Kill switch is on — no live orders", "mode": "live"}
    res = a.place_live_order_with_retry(
        txn, tradingsymbol, int(qty), reason=f"{model}_{tag}", max_retries=1, user_id=uid
    )
    if not res.get("ok"):
        return {"ok": False, "message": res.get("error") or "order failed", "mode": "live"}
    live = not res.get("dry_run")
    fill = None
    if live:
        fill = a.fetch_order_average_price(res.get("order_id"), user_id=uid)
    return {
        "ok": True,
        "mode": "live" if live else "paper",
        "order_id": str(res.get("order_id") or ""),
        "fill_price": float(fill or mark),
        "fill_source": "kite_average_price" if fill else ("pre_order_ltp" if live else "paper_ltp"),
        "tradingsymbol": tradingsymbol,
        "transaction_type": txn,
        "quantity": int(qty),
    }


def broker_net_qty(tradingsymbol: str) -> int | None:
    """Net quantity Zerodha actually holds for a symbol (None if it can't be read)."""
    try:
        for p in (kite().positions() or {}).get("net", []):
            if p.get("tradingsymbol") == tradingsymbol:
                return int(p.get("quantity") or 0)
        return 0
    except Exception:
        return None


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> str:
    return now_ist().strftime("%Y-%m-%d")


def parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None
