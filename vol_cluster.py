"""VolCluster Nifty Futures — Adaptive SuperTrend on closed 1H NIFTY bars, always in.

Moved from STAIRS (vol_cluster_engine / _signal / _state / _runner). Keep params
A33 / B2.5 / C2 / S0.6. Hold overnight; reverse only on a closed-bar ST flip.
Paper and live keep separate books; the model's Go-Live mode picks which one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

import kite_feed as kf

MODEL = "vol_cluster"
LABEL = "VolCluster FUT"

ATR_PERIOD = 33
BASE_MULT = 2.5
N_CLUSTERS = 2
CLUSTER_SENS = 0.6
PCTL_WINDOW = 100
KEEP_LABEL = "A33 / B2.5 / C2 / S0.6"
MIN_BARS = 150  # ATR 33 + pctl 100 warm-up
ROLL_FROM_HHMM = 1500

DEFAULT_CONFIG: dict[str, Any] = {
    "capital": 500_000.0,
    "nifty_start": 24_000.0,
    "leverage": 2.0,
    "lot_size": 65,
    "lots": 1,
}


# ── engine ────────────────────────────────────────────────────────────────────

def keep_params() -> dict[str, Any]:
    return {
        "method": "VolCluster",
        "atr_period": ATR_PERIOD,
        "base_mult": BASE_MULT,
        "n_clusters": N_CLUSTERS,
        "cluster_sens": CLUSTER_SENS,
        "pctl_window": PCTL_WINDOW,
        "label": KEEP_LABEL,
    }


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _atr_percentile(atr_s: pd.Series, window: int = 100) -> pd.Series:
    def pct(x: np.ndarray) -> float:
        if len(x) < 5 or not np.isfinite(x[-1]):
            return 50.0
        return float((x <= x[-1]).mean() * 100)

    return atr_s.rolling(window, min_periods=20).apply(pct, raw=True)


def vol_cluster_multiplier(atr_s: pd.Series) -> tuple[pd.Series, pd.Series]:
    n_clusters = max(2, int(N_CLUSTERS))
    pctl = _atr_percentile(atr_s, PCTL_WINDOW).fillna(50.0) / 100.0
    cluster = np.floor(pctl.to_numpy(dtype=float) * n_clusters).astype(int)
    cluster = np.clip(cluster, 0, n_clusters - 1)
    mid = (n_clusters - 1) / 2.0
    spread = CLUSTER_SENS * BASE_MULT
    mult = BASE_MULT + (cluster - mid) / max(mid, 1e-9) * spread
    mult_s = pd.Series(mult, index=atr_s.index).clip(lower=0.5, upper=BASE_MULT * 3.0)
    return mult_s, pd.Series(cluster, index=atr_s.index)


def supertrend_mult_series(df: pd.DataFrame, period: int, multipliers: pd.Series) -> pd.DataFrame:
    out = df.copy()
    n = len(out)
    atr_v = atr(out, period).to_numpy(dtype=float)
    mult = pd.Series(multipliers, index=out.index).astype(float).to_numpy()
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    close = out["close"].to_numpy(dtype=float)
    hl2 = (high + low) / 2.0
    basic_ub = hl2 + mult * atr_v
    basic_lb = hl2 - mult * atr_v

    final_ub = np.full(n, np.nan)
    final_lb = np.full(n, np.nan)
    st = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)

    for i in range(n):
        if not np.isfinite(atr_v[i]) or not np.isfinite(mult[i]):
            if i > 0:
                direction[i] = direction[i - 1]
                st[i] = st[i - 1]
                final_ub[i] = final_ub[i - 1]
                final_lb[i] = final_lb[i - 1]
            continue
        if i == 0 or not np.isfinite(final_ub[i - 1]):
            final_ub[i] = basic_ub[i]
        elif basic_ub[i] < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]:
            final_ub[i] = basic_ub[i]
        else:
            final_ub[i] = final_ub[i - 1]

        if i == 0 or not np.isfinite(final_lb[i - 1]):
            final_lb[i] = basic_lb[i]
        elif basic_lb[i] > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]:
            final_lb[i] = basic_lb[i]
        else:
            final_lb[i] = final_lb[i - 1]

        if i == 0 or not np.isfinite(st[i - 1]):
            direction[i] = 1
            st[i] = final_lb[i]
            continue

        if direction[i - 1] >= 0:
            if close[i] < final_lb[i]:
                direction[i], st[i] = -1, final_ub[i]
            else:
                direction[i], st[i] = 1, final_lb[i]
        else:
            if close[i] > final_ub[i]:
                direction[i], st[i] = 1, final_lb[i]
            else:
                direction[i], st[i] = -1, final_ub[i]

    out["st_dir"] = direction
    out["st_line"] = st
    out["atr"] = atr_v
    out["adaptive_mult"] = mult
    return out


def compute_vol_cluster(df: pd.DataFrame) -> pd.DataFrame:
    atr_s = atr(df, ATR_PERIOD)
    mult, cluster = vol_cluster_multiplier(atr_s)
    out = supertrend_mult_series(df, ATR_PERIOD, mult)
    out["cluster"] = cluster
    out["atr_raw"] = atr_s
    return out


# ── signal ────────────────────────────────────────────────────────────────────

def _closed_bars(bars: pd.DataFrame, bar_minutes: int = 60) -> pd.DataFrame:
    """Drop the forming candle (Kite stamps bars at their open time)."""
    if bars is None or len(bars) < 2:
        return bars
    last = pd.Timestamp(bars.index[-1])
    now = pd.Timestamp(kf.now_ist().replace(tzinfo=None))
    if last + pd.Timedelta(minutes=bar_minutes) > now:
        return bars.iloc[:-1]
    return bars


def signal() -> dict[str, Any]:
    params = keep_params()
    try:
        df = kf.index_bars(kf.NIFTY_TOKEN, "60minute", 180)
    except Exception as exc:
        return {"ok": False, "direction": "FLAT", "message": f"Kite 1H bars unavailable: {exc}",
                "params": params, "data_source": "none"}
    if df is None or len(df) < MIN_BARS:
        return {"ok": False, "direction": "FLAT",
                "message": f"Insufficient 1H bars (need ~{MIN_BARS})",
                "bars": 0 if df is None else len(df), "params": params, "data_source": "kite_60minute"}

    bars = _closed_bars(compute_vol_cluster(df))
    last, prev = bars.iloc[-1], bars.iloc[-2]
    dir_i = int(last["st_dir"]) if pd.notna(last["st_dir"]) else 0
    prev_dir = int(prev["st_dir"]) if pd.notna(prev["st_dir"]) else dir_i
    direction = "LONG" if dir_i > 0 else ("SHORT" if dir_i < 0 else "FLAT")

    spot = kf.nifty_spot() or float(last["close"])
    fut: dict[str, Any] = {}
    fut_ltp = 0.0
    try:
        fut = kf.futures_contract()
        fut_ltp = kf.nfo_ltp(fut["tradingsymbol"])
    except Exception:
        pass
    mark = fut_ltp if fut_ltp > 0 else spot

    def r(v):
        return round(float(v), 2) if pd.notna(v) else None

    return {
        "ok": True,
        "symbol": "NIFTY",
        "direction": direction,
        "st_dir": dir_i,
        "prev_st_dir": prev_dir,
        "signal_change": dir_i != prev_dir and dir_i != 0,
        "st": r(last["st_line"]),
        "mult": r(last["adaptive_mult"]),
        "cluster": int(last["cluster"]) if pd.notna(last["cluster"]) else None,
        "atr": r(last["atr"]),
        "spot": round(spot, 2),
        "mark": round(mark, 2),
        "futures_ltp": round(fut_ltp, 2) if fut_ltp > 0 else None,
        "futures_symbol": fut.get("tradingsymbol"),
        "futures_expiry": fut.get("expiry"),
        "close": round(float(last["close"]), 2),
        "bar_time": str(bars.index[-1]),
        "bars": len(bars),
        "params": params,
        "keep_label": KEEP_LABEL,
        "data_source": "kite_60minute",
        "mark_source": "kite_futures" if fut_ltp > 0 else "kite_spot",
    }


# ── book (kv; one per phase) ─────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def phase() -> str:
    return "live" if kf.model_mode(MODEL) == "live" else "paper"


def book_key(ph: str | None = None) -> str:
    return f"{MODEL}_book::{ph or phase()}"


def _empty() -> dict[str, Any]:
    return {"config": dict(DEFAULT_CONFIG), "open_position": None, "trades": [], "orders": [],
            "peak_capital": float(DEFAULT_CONFIG["capital"]), "updated_at": _now_iso()}


def _load(ph: str | None = None) -> dict[str, Any]:
    ph = ph or phase()
    data = kf.A().kv_get(book_key(ph), None)
    if not isinstance(data, dict):
        data = _empty()
        if ph == "live":
            paper = kf.A().kv_get(book_key("paper"), None)
            if isinstance(paper, dict) and isinstance(paper.get("config"), dict):
                data["config"] = dict(paper["config"])
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (data.get("config") or {}).items():
        if k in DEFAULT_CONFIG:
            cfg[k] = v
    return {
        "config": cfg,
        "open_position": data.get("open_position"),
        "trades": list(data.get("trades") or []),
        "orders": list(data.get("orders") or []),
        "peak_capital": float(data.get("peak_capital") or cfg["capital"]),
        "updated_at": data.get("updated_at") or _now_iso(),
    }


def _save(state: dict[str, Any], ph: str | None = None) -> None:
    state["updated_at"] = _now_iso()
    state["orders"] = list(state.get("orders") or [])[-500:]
    kf.A().kv_set(book_key(ph), state)


def get_config() -> dict[str, Any]:
    return dict(_load()["config"])


def set_config(updates: dict[str, Any]) -> dict[str, Any]:
    state = _load()
    cfg = dict(state["config"])
    for k, v in (updates or {}).items():
        if k not in DEFAULT_CONFIG:
            continue
        if k in ("capital", "nifty_start", "leverage"):
            cfg[k] = float(v)
        elif k in ("lot_size", "lots"):
            cfg[k] = max(1, int(v))
    state["config"] = cfg
    if not state["trades"] and state["open_position"] is None:
        state["peak_capital"] = float(cfg["capital"])
    _save(state)
    return dict(cfg)


def derived_sizing(cfg: dict[str, Any]) -> dict[str, Any]:
    lot_size = max(1, int(cfg.get("lot_size") or 65))
    lots = max(1, int(cfg.get("lots") or 1))
    leverage = max(1.0, float(cfg.get("leverage") or 2))
    nifty = float(cfg.get("nifty_start") or 24_000)
    return {
        "qty": lots * lot_size,
        "lots": lots,
        "lot_size": lot_size,
        "leverage": leverage,
        "min_capital_2lots": round((nifty * lot_size * 2) / leverage, 2),
        "capital": float(cfg.get("capital") or 0),
        "nifty_start": nifty,
    }


def _summary(state: dict[str, Any], mark: float | None = None) -> dict[str, Any]:
    start = float(state["config"].get("capital") or 0)
    trades = state.get("trades") or []
    realized = sum(float(t.get("pnl") or 0) for t in trades)
    running = start + realized
    peak = max(float(state.get("peak_capital") or start), running)
    open_pnl = 0.0
    pos = state.get("open_position")
    if isinstance(pos, dict) and mark and mark > 0:
        entry, qty = float(pos.get("entry") or 0), int(pos.get("qty") or 0)
        if entry > 0 and qty > 0:
            open_pnl = ((mark - entry) if str(pos.get("side")).upper() == "LONG" else (entry - mark)) * qty
    equity = running + open_pnl
    peak = max(peak, equity)
    max_dd = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
    for t in trades:
        max_dd = max(max_dd, abs(float(t.get("drawdown") or 0)))
    wins = [float(t["pnl"]) for t in trades if float(t.get("pnl") or 0) > 0]
    losses = [float(t["pnl"]) for t in trades if float(t.get("pnl") or 0) < 0]
    return {
        "start_capital": start,
        "curr_capital": round(running, 2),
        "equity": round(equity, 2),
        "realized_pnl": round(realized, 2),
        "open_pnl": round(open_pnl, 2),
        "return_on_cap": round((realized + open_pnl) / start, 4) if start > 0 else 0.0,
        "max_dd": round(max_dd, 4),
        "curr_dd": round((peak - equity) / peak, 4) if peak > 0 and equity < peak else 0.0,
        "peak_capital": round(peak, 2),
        "trade_count": len(trades),
        "won": len(wins),
        "lost": len(losses),
        "big_win": max(wins) if wins else None,
        "big_loss": min(losses) if losses else None,
        "open": isinstance(pos, dict),
    }


def get_open_position() -> dict[str, Any] | None:
    pos = _load().get("open_position")
    return dict(pos) if isinstance(pos, dict) else None


def _record_order(state: dict[str, Any], order: dict[str, Any], tag: str) -> None:
    state.setdefault("orders", []).append({
        "id": order.get("order_id"), "ts": _now_iso(), "tag": tag, "mode": order.get("mode"),
        "tradingsymbol": order.get("tradingsymbol"), "transaction_type": order.get("transaction_type"),
        "quantity": order.get("quantity"), "fill_price": order.get("fill_price"),
        "fill_source": order.get("fill_source"),
    })


def _open(state: dict[str, Any], *, side: str, order: dict[str, Any], contract: dict[str, Any], action: str) -> dict[str, Any]:
    if state.get("open_position"):
        raise ValueError("Already in a position — use flip or flat first")
    pos = {
        "id": f"VC-{uuid.uuid4().hex[:8].upper()}",
        "side": side,
        "entry": round(float(order["fill_price"]), 2),
        "qty": int(order["quantity"]),
        "entry_ts": _now_iso(),
        "entry_date": kf.today_ist(),
        "contract": contract.get("tradingsymbol") or "",
        "contract_expiry": contract.get("expiry") or "",
        "order_id": order.get("order_id") or "",
        "mode": order.get("mode") or "paper",
        "action": action,
    }
    state["open_position"] = pos
    _record_order(state, order, f"open_{action.lower()}")
    return pos


def _close(state: dict[str, Any], *, order: dict[str, Any], action: str) -> dict[str, Any]:
    pos = state.get("open_position")
    if not isinstance(pos, dict):
        raise ValueError("No open position to close")
    exit_price = float(order["fill_price"])
    entry, qty, side = float(pos["entry"]), int(pos["qty"]), str(pos["side"]).upper()
    pts = (exit_price - entry) if side == "LONG" else (entry - exit_price)
    pnl = pts * qty
    start = float(state["config"].get("capital") or 0)
    running = start + sum(float(t.get("pnl") or 0) for t in state.get("trades") or []) + pnl
    peak = max(float(state.get("peak_capital") or start), running)
    trade = {
        "id": pos.get("id") or f"VC-{uuid.uuid4().hex[:8].upper()}",
        "entry_date": pos.get("entry_date") or "",
        "exit_date": kf.today_ist(),
        "trend": side,
        "entry": round(entry, 2),
        "exit": round(exit_price, 2),
        "action": action,
        "action_type": action,
        "qty": qty,
        "pnl": round(pnl, 2),
        "net_pts": round(pts, 2),
        "running_cap": round(running, 2),
        "peak": round(peak, 2),
        "drawdown": round((peak - running) / peak, 4) if peak > 0 and running < peak else 0.0,
        "trade_return": round(pnl / start, 4) if start > 0 else 0.0,
        "contract": pos.get("contract") or "",
        "mode": pos.get("mode") or "paper",
        "entry_order_id": pos.get("order_id") or "",
        "exit_order_id": order.get("order_id") or "",
        "closed_ts": _now_iso(),
    }
    state["trades"] = (list(state.get("trades") or []) + [trade])[-2000:]
    state["open_position"] = None
    state["peak_capital"] = peak
    _record_order(state, order, f"close_{action.lower()}")
    return trade


def panel(mark: float | None = None) -> dict[str, Any]:
    state = _load()
    pos = state.get("open_position")
    if mark is None and isinstance(pos, dict):
        mark = kf.nfo_ltp(str(pos.get("contract") or "")) or None
    return {
        "config": state["config"],
        "sizing": derived_sizing(state["config"]),
        "open_position": pos,
        "mark": mark,
        "trades": list(reversed(state["trades"][-200:])),
        "orders": list(reversed(state["orders"][-50:])),
        "summary": _summary(state, mark=mark),
        "phase": phase(),
        "mode": kf.model_mode(MODEL),
        "updated_at": state.get("updated_at"),
    }


def open_count() -> int:
    return 1 if get_open_position() else 0


# ── runner ────────────────────────────────────────────────────────────────────

def _txn(side: str, closing: bool = False) -> str:
    buy = side == "LONG"
    return ("SELL" if buy else "BUY") if closing else ("BUY" if buy else "SELL")


def _contract_expiry(tsym: str) -> str:
    try:
        for row in kf.A().get_nfo_instruments():
            if row.get("tradingsymbol") == tsym:
                e = row.get("expiry")
                return e.isoformat() if hasattr(e, "isoformat") else str(e)
    except Exception:
        pass
    return ""


def _close_order(pos: dict[str, Any], tag: str) -> dict[str, Any]:
    tsym = str(pos.get("contract") or "")
    if not tsym:
        return {"ok": False, "message": "Open position has no contract symbol"}
    mark = kf.nfo_ltp(tsym) or float(pos.get("entry") or 0)
    return kf.place(MODEL, _txn(str(pos["side"]).upper(), closing=True), tsym, int(pos["qty"]), mark, tag)


def _open_order(side: str, qty: int, tag: str) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = kf.futures_contract(skip_near_expiry=True)
    mark = kf.nfo_ltp(contract["tradingsymbol"])
    if mark <= 0:
        return {"ok": False, "message": f"No LTP for {contract['tradingsymbol']}"}, contract
    return kf.place(MODEL, _txn(side), contract["tradingsymbol"], qty, mark, tag), contract


def place_action(action: str) -> dict[str, Any]:
    """enter | flip | flat on the current closed-bar signal."""
    action = (action or "").strip().lower()
    if action not in ("enter", "flip", "flat"):
        return {"ok": False, "message": "action must be enter | flip | flat"}
    sig = signal()
    if action != "flat" and not sig.get("ok"):
        return {"ok": False, "message": sig.get("message") or "Signal unavailable", "signal": sig}
    direction = str(sig.get("direction") or "FLAT").upper()
    state = _load()
    pos = state.get("open_position")
    qty = int(derived_sizing(state["config"])["qty"])

    try:
        if action == "enter":
            if pos:
                return {"ok": False, "message": "Already in a position — use Flip or Flat"}
            if direction not in ("LONG", "SHORT"):
                return {"ok": False, "message": f"No directional signal ({direction})"}
            order, contract = _open_order(direction, qty, "enter")
            if not order.get("ok"):
                return order
            opened = _open(state, side=direction, order=order, contract=contract, action="Enter")
            _save(state)
            return {"ok": True, "action": "enter", "order": order, "open_position": opened, "signal": sig}

        if action == "flat":
            if not pos:
                return {"ok": False, "message": "No open position to flatten"}
            order = _close_order(pos, "flat")
            if not order.get("ok"):
                return order
            closed = _close(state, order=order, action="Flat")
            _save(state)
            return {"ok": True, "action": "flat", "order": order, "closed": closed, "signal": sig}

        if not pos:
            return {"ok": False, "message": "No open position to flip — use Enter"}
        if direction not in ("LONG", "SHORT"):
            return {"ok": False, "message": f"Cannot flip without LONG/SHORT signal ({direction})"}
        cur = str(pos.get("side") or "").upper()
        if direction == cur:
            return {"ok": False, "message": f"Signal still {direction} — wait for ST flip before reversing"}
        close_order = _close_order(pos, "flip_close")
        if not close_order.get("ok"):
            return close_order
        closed = _close(state, order=close_order, action="Reversal")
        _save(state)
        open_order, contract = _open_order(direction, qty, "flip_open")
        if not open_order.get("ok"):
            kf.log(f"VolCluster: closed {cur} but failed to open {direction}: {open_order.get('message')}", level="ERROR")
            return {"ok": False, "message": f"Closed {cur} but failed to open {direction}: {open_order.get('message')}",
                    "closed": closed}
        opened = _open(state, side=direction, order=open_order, contract=contract, action="Reversal")
        _save(state)
        return {"ok": True, "action": "flip", "closed": closed, "opened": opened, "signal": sig}
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}


def _maybe_roll(pos: dict[str, Any]) -> dict[str, Any] | None:
    """On the contract's expiry day from 15:00, move the position to the next month."""
    now = kf.now_ist()
    if now.hour * 100 + now.minute < ROLL_FROM_HHMM:
        return None
    exp = kf.parse_date(pos.get("contract_expiry") or _contract_expiry(str(pos.get("contract") or "")))
    if exp is None or exp > now.date():
        return None
    state = _load()
    side = str(pos["side"]).upper()
    close_order = _close_order(pos, "roll_close")
    if not close_order.get("ok"):
        return {"ok": False, "phase": "roll_failed", "message": close_order.get("message")}
    closed = _close(state, order=close_order, action="Roll-over")
    _save(state)
    contract = kf.futures_contract(skip_near_expiry=True)
    if contract["tradingsymbol"] == pos.get("contract"):
        return {"ok": False, "phase": "roll_failed", "message": "No next-month contract to roll into", "closed": closed}
    mark = kf.nfo_ltp(contract["tradingsymbol"])
    open_order = kf.place(MODEL, _txn(side), contract["tradingsymbol"], int(pos["qty"]), mark, "roll_open")
    if not open_order.get("ok"):
        kf.log(f"VolCluster: roll closed {pos.get('contract')} but failed to open {contract['tradingsymbol']}", level="ERROR")
        return {"ok": False, "phase": "roll_failed", "message": open_order.get("message"), "closed": closed}
    opened = _open(state, side=side, order=open_order, contract=contract, action="Roll-over")
    _save(state)
    return {"ok": True, "phase": "rolled", "closed": closed, "opened": opened}


def auto_tick() -> dict[str, Any]:
    """Flat + LONG/SHORT → Enter; open + opposite closed-bar direction → Flip; else Hold."""
    now = kf.now_ist()
    stamp = now.strftime("%H:%M:%S")
    if kf.model_mode(MODEL) == "off":
        return {"ok": True, "phase": "model_off", "now_ist": stamp}
    hhmm = now.hour * 100 + now.minute
    if now.weekday() >= 5 or hhmm < 920 or hhmm > 1530:
        return {"ok": True, "phase": "outside_session", "now_ist": stamp}

    pos = get_open_position()
    if pos:
        rolled = _maybe_roll(pos)
        if rolled is not None:
            return {**rolled, "now_ist": stamp}

    sig = signal()
    if not sig.get("ok"):
        return {"ok": False, "phase": "signal_error", "message": sig.get("message"), "now_ist": stamp}
    direction = str(sig.get("direction") or "FLAT").upper()
    if not pos:
        if direction not in ("LONG", "SHORT"):
            return {"ok": True, "phase": "flat_no_signal", "now_ist": stamp}
        res = place_action("enter")
        return {"ok": bool(res.get("ok")), "phase": "enter_attempt", "now_ist": stamp, "actions": [res]}
    cur = str(pos.get("side") or "").upper()
    if direction in ("LONG", "SHORT") and direction != cur:
        res = place_action("flip")
        return {"ok": bool(res.get("ok")), "phase": "flip_attempt", "now_ist": stamp, "actions": [res]}
    return {"ok": True, "phase": "hold", "message": f"Holding {cur} — ST {direction}", "now_ist": stamp}


def kill_exit() -> int:
    """Go-Live Kill & Exit: close the live book's open position against the real
    broker quantity; never place a blind order if Zerodha shows nothing open."""
    state = _load()
    pos = state.get("open_position")
    if not isinstance(pos, dict):
        return 0
    tsym = str(pos.get("contract") or "")
    if phase() == "live":
        net = kf.broker_net_qty(tsym)
        if net:
            txn = "SELL" if net > 0 else "BUY"
            order = kf.place(MODEL, txn, tsym, abs(net), kf.nfo_ltp(tsym) or float(pos["entry"]), "kill_exit")
        else:
            kf.log(f"VolCluster kill-exit: broker shows no open {tsym} — nothing to close, book marked flat", level="WARNING")
            order = {"ok": True, "mode": "live", "order_id": "", "fill_price": kf.nfo_ltp(tsym) or float(pos["entry"]),
                     "tradingsymbol": tsym, "transaction_type": "", "quantity": int(pos["qty"])}
    else:
        order = _close_order(pos, "kill_exit")
    if not order.get("ok"):
        return 0
    _close(state, order=order, action="Kill-exit")
    _save(state)
    return 1
