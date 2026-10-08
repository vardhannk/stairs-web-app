"""GC Options Buy (S7) — Golden Cross 15m NIFTY option buying, one entry per day.

Moved from STAIRS (golden_cross_buy / gc_ob_state / gc_ob_runner). Signal:
SMA50 vs SMA200 regime + Donchian(20) break + rising/falling EMA20, executed on
the next bar. BUY the ATM CE (bull) / PE (bear), sized by a VIX risk fraction.
Exits: 50% at +15% premium, rest on ATR trail / VIX-scaled final TP / ₹15k hard
stop / 15:25 square-off. Paper and live keep separate books.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time as dtime, timezone
from typing import Any

import numpy as np
import pandas as pd

import kite_feed as kf

MODEL = "gc_options_buy"
LABEL = "GC Options Buy"

SMA_FAST = 50
SMA_SLOW = 200
DONCHIAN_PERIOD = 20
EMA_TREND = 20
MIN_BARS = SMA_SLOW + DONCHIAN_PERIOD + 5

BUY_VIX_LOW = 13.0
BUY_VIX_MED = 17.0
BUY_RISK_LOW = 0.15
BUY_RISK_MED = 0.10
BUY_RISK_HIGH = 0.05
BUY_PARTIAL_TP_PCT = 0.15
BUY_FINAL_TP_PCT_LOW = 0.20
BUY_FINAL_TP_PCT_HIGH = 0.40
BUY_ATR_MULTIPLIER = 2.5
BUY_MAX_LOSS = 15_000.0
ENTRY_CUTOFF = dtime(15, 0)
EOD_SQUARE = dtime(15, 25)
DEFAULT_VIX = 16.0

DEFAULT_CONFIG: dict[str, Any] = {"capital": 500_000.0, "lot_size": 65, "auto_daily": False}


# ── signal ────────────────────────────────────────────────────────────────────

def _vix_risk_fraction(vix: float) -> float:
    if vix <= BUY_VIX_LOW:
        return BUY_RISK_LOW
    if vix <= BUY_VIX_MED:
        return BUY_RISK_MED
    return BUY_RISK_HIGH


def _vix_final_tp(vix: float) -> float:
    if vix <= BUY_VIX_LOW:
        return BUY_FINAL_TP_PCT_LOW
    if vix > BUY_VIX_MED:
        return BUY_FINAL_TP_PCT_HIGH
    r = (vix - BUY_VIX_LOW) / (BUY_VIX_MED - BUY_VIX_LOW)
    return BUY_FINAL_TP_PCT_LOW + r * (BUY_FINAL_TP_PCT_HIGH - BUY_FINAL_TP_PCT_LOW)


def compute_quantity(*, capital: float, risk_fraction: float, premium: float, lot_size: int = 65) -> dict[str, Any]:
    """Risk budget = capital × VIX risk fraction; lots = floor(budget / (premium × lot))."""
    lot = max(1, int(lot_size))
    budget = float(capital) * max(0.0, float(risk_fraction))
    cost_per_lot = float(premium) * lot
    lots = int(budget // cost_per_lot) if cost_per_lot > 0 else 0
    return {"risk_fraction": float(risk_fraction), "risk_budget": round(budget, 2),
            "cost_per_lot": round(cost_per_lot, 2), "lots": lots, "quantity": lots * lot, "lot_size": lot}


def _bars15() -> pd.DataFrame:
    return kf.index_bars(kf.NIFTY_TOKEN, "15minute", 60)


def _compute_signals(bars15: pd.DataFrame) -> pd.DataFrame:
    out = bars15.copy()
    out["sma_fast"] = out["close"].rolling(SMA_FAST, min_periods=SMA_FAST).mean()
    out["sma_slow"] = out["close"].rolling(SMA_SLOW, min_periods=SMA_SLOW).mean()
    out["don_high"] = out["high"].rolling(DONCHIAN_PERIOD, min_periods=DONCHIAN_PERIOD).max().shift(1)
    out["don_low"] = out["low"].rolling(DONCHIAN_PERIOD, min_periods=DONCHIAN_PERIOD).min().shift(1)
    out["ema20"] = out["close"].ewm(span=EMA_TREND, adjust=False).mean()
    out["ema20_lag"] = out["ema20"].shift(1)
    bull = out["sma_fast"] > out["sma_slow"]
    bear = out["sma_fast"] < out["sma_slow"]
    raw = np.zeros(len(out), dtype=int)
    raw[np.where(bull & (out["close"] > out["don_high"]) & (out["ema20"] > out["ema20_lag"]))[0]] = 1
    raw[np.where(bear & (out["close"] < out["don_low"]) & (out["ema20"] < out["ema20_lag"]))[0]] = -1
    out["raw_signal"] = raw
    out["signal"] = out["raw_signal"].shift(1).fillna(0).astype(int)
    return out


def signal() -> dict[str, Any]:
    try:
        df = _bars15()
    except Exception as exc:
        return {"ok": False, "signal": "FLAT", "message": f"Kite 15m bars unavailable: {exc}", "data_source": "none"}
    if df is None or len(df) < MIN_BARS:
        return {"ok": False, "signal": "FLAT", "message": f"Insufficient 15m bars (need ~{MIN_BARS})",
                "bars": 0 if df is None else len(df), "data_source": "kite_15minute"}

    bars = _compute_signals(df)
    last, prev = bars.iloc[-1], bars.iloc[-2]
    sig_i = int(last["signal"])
    if sig_i == 1:
        sig, regime = "CE", "BULL"
    elif sig_i == -1:
        sig, regime = "PE", "BEAR"
    else:
        sig = "FLAT"
        regime = ("BULL" if last["sma_fast"] > last["sma_slow"] else "BEAR") \
            if pd.notna(last["sma_fast"]) and pd.notna(last["sma_slow"]) else "N/A"

    before_cutoff = kf.now_ist().time().replace(tzinfo=None) < ENTRY_CUTOFF
    allow_entry = sig in ("CE", "PE") and before_cutoff
    vix = kf.india_vix(DEFAULT_VIX)
    risk_frac = _vix_risk_fraction(vix)
    final_tp = _vix_final_tp(vix)
    spot = kf.nifty_spot() or float(last["close"])
    atm = int(round(spot / kf.NIFTY_STRIKE_STEP) * kf.NIFTY_STRIKE_STEP)
    try:
        expiry = kf.nearest_option_expiry()
    except Exception:
        expiry = ""

    def r(v):
        return round(float(v), 2) if pd.notna(v) else None

    return {
        "ok": True,
        "symbol": "NIFTY",
        "signal": sig,
        "regime": regime,
        "raw_signal_last_bar": int(last["raw_signal"]),
        "allow_entry": allow_entry,
        "entry_blocked_reason": None if allow_entry else (
            "No CE/PE breakout signal (lagged)" if sig == "FLAT"
            else f"Past entry cutoff {ENTRY_CUTOFF.strftime('%H:%M')} IST"),
        "spot": round(spot, 2),
        "atm_strike": atm,
        "expiry": expiry,
        "lot_size": DEFAULT_CONFIG["lot_size"],
        "suggested_option_type": sig if sig in ("CE", "PE") else None,
        "vix": round(vix, 2),
        "risk_fraction": risk_frac,
        "risk_pct": round(risk_frac * 100, 1),
        "partial_tp_pct": BUY_PARTIAL_TP_PCT * 100,
        "final_tp_pct": round(final_tp * 100, 1),
        "atr_multiplier": BUY_ATR_MULTIPLIER,
        "max_loss_inr": BUY_MAX_LOSS,
        "eod_square_off": EOD_SQUARE.strftime("%H:%M"),
        "entry_cutoff": ENTRY_CUTOFF.strftime("%H:%M"),
        "sma_fast": r(last["sma_fast"]),
        "sma_slow": r(last["sma_slow"]),
        "ema20": r(last["ema20"]),
        "don_high": r(last["don_high"]),
        "don_low": r(last["don_low"]),
        "bar_time": str(bars.index[-1]),
        "prev_close": round(float(prev["close"]), 2),
        "bars_15m": len(bars),
        "data_source": "kite_15minute",
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
            "peak_capital": float(DEFAULT_CONFIG["capital"]), "last_entry_date": None,
            "last_flat_date": None, "updated_at": _now_iso()}


def _peak_from_path(state: dict[str, Any]) -> float:
    start = float(state["config"].get("capital") or 0)
    peak = running = start
    for t in state.get("trades") or []:
        running += float(t.get("pnl") or 0)
        peak = max(peak, running)
    return peak


def _load(ph: str | None = None) -> dict[str, Any]:
    ph = ph or phase()
    data = kf.A().kv_get(book_key(ph), None)
    if not isinstance(data, dict):
        data = _empty()
        if ph == "live":
            paper = kf.A().kv_get(book_key("paper"), None)
            if isinstance(paper, dict) and isinstance(paper.get("config"), dict):
                data["config"] = {**dict(paper["config"]), "auto_daily": False}
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
        "last_entry_date": data.get("last_entry_date"),
        "last_flat_date": data.get("last_flat_date"),
        "updated_at": data.get("updated_at") or _now_iso(),
    }


def _save(state: dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    state["orders"] = list(state.get("orders") or [])[-500:]
    kf.A().kv_set(book_key(), state)


def get_config() -> dict[str, Any]:
    return dict(_load()["config"])


def set_config(updates: dict[str, Any]) -> dict[str, Any]:
    state = _load()
    cfg = dict(state["config"])
    capital_changed = False
    for k, v in (updates or {}).items():
        if k == "capital":
            capital_changed = abs(float(v) - float(cfg.get("capital") or 0)) > 0.01
            cfg[k] = float(v)
        elif k == "lot_size":
            cfg[k] = max(1, int(v))
        elif k == "auto_daily":
            cfg[k] = bool(v)
    state["config"] = cfg
    if capital_changed or (not state["trades"] and state["open_position"] is None):
        state["peak_capital"] = _peak_from_path(state)
    _save(state)
    return dict(cfg)


def calc_pnl(entry: float, exit_px: float, qty: int) -> float:
    return (float(exit_px) - float(entry)) * int(qty)


def get_open_position() -> dict[str, Any] | None:
    pos = _load().get("open_position")
    return dict(pos) if isinstance(pos, dict) else None


def curr_capital() -> float:
    state = _load()
    return float(state["config"].get("capital") or 0) + sum(float(t.get("pnl") or 0) for t in state["trades"])


def _summary(state: dict[str, Any], mark: float | None = None) -> dict[str, Any]:
    start = float(state["config"].get("capital") or 0)
    trades = state.get("trades") or []
    realized = sum(float(t.get("pnl") or 0) for t in trades)
    running = start + realized
    peak = max(float(state.get("peak_capital") or start), running)
    open_pnl = 0.0
    pos = state.get("open_position")
    if isinstance(pos, dict) and mark and mark > 0:
        open_pnl = calc_pnl(float(pos.get("entry") or 0), float(mark), int(pos.get("qty") or 0))
    equity = running + open_pnl
    peak = max(peak, equity)
    max_dd = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
    for t in trades:
        max_dd = max(max_dd, abs(float(t.get("drawdown") or 0)))
    wins = [float(t["pnl"]) for t in trades if float(t.get("pnl") or 0) > 0]
    losses = [float(t["pnl"]) for t in trades if float(t.get("pnl") or 0) < 0]
    closed = len(wins) + len(losses)
    return {
        "start_capital": start,
        "curr_capital": round(running, 2),
        "equity": round(equity, 2),
        "realized_pnl": round(realized, 2),
        "open_pnl": round(open_pnl, 2),
        "return_on_cap": round((realized + open_pnl) / start, 4) if start else 0.0,
        "max_dd": round(max_dd, 4),
        "curr_dd": round((peak - equity) / peak, 4) if peak > 0 and equity < peak else 0.0,
        "peak_capital": round(peak, 2),
        "trade_count": len(trades),
        "won": len(wins),
        "lost": len(losses),
        "win_ratio": round(len(wins) / closed, 4) if closed else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "big_win": round(max(wins), 2) if wins else None,
        "big_loss": round(min(losses), 2) if losses else None,
        "open": isinstance(pos, dict),
        "last_entry_date": state.get("last_entry_date"),
    }


def _record_order(state: dict[str, Any], order: dict[str, Any], tag: str) -> None:
    state.setdefault("orders", []).append({
        "id": order.get("order_id"), "ts": _now_iso(), "tag": tag, "mode": order.get("mode"),
        "tradingsymbol": order.get("tradingsymbol"), "transaction_type": order.get("transaction_type"),
        "quantity": order.get("quantity"), "fill_price": order.get("fill_price"),
        "fill_source": order.get("fill_source"),
    })


def _patch_open(updates: dict[str, Any]) -> dict[str, Any] | None:
    state = _load()
    pos = state.get("open_position")
    if not isinstance(pos, dict):
        return None
    pos = {**pos, **(updates or {})}
    state["open_position"] = pos
    _save(state)
    return pos


def _close(*, order: dict[str, Any], exit_price: float, exit_reason: str) -> dict[str, Any]:
    state = _load()
    pos = state.get("open_position")
    if not isinstance(pos, dict):
        raise ValueError("No open GC option position")
    entry = float(pos["entry"])
    remain_qty = int(pos["qty"])
    partial_booked = bool(pos.get("partial_booked"))
    partial_qty = int(pos.get("partial_qty") or 0)
    partial_exit = float(pos.get("partial_exit_price") or 0)
    partial_pnl = float(pos.get("partial_pnl") or 0)
    final_pnl = calc_pnl(entry, exit_price, remain_qty)
    with_partial = partial_booked and partial_qty > 0
    total_pnl = round(partial_pnl + final_pnl, 2) if with_partial else round(final_pnl, 2)
    total_qty = partial_qty + remain_qty if with_partial else remain_qty

    start = float(state["config"].get("capital") or 0)
    prev_cap = start + sum(float(t.get("pnl") or 0) for t in state["trades"])
    running = prev_cap + total_pnl
    old_peak = _peak_from_path(state)
    peak = max(old_peak, running)
    reason = f"Partial TP (+15%) + {exit_reason}" if with_partial else (exit_reason or "Flat")
    trade: dict[str, Any] = {
        "id": str(pos.get("id") or ""),
        "entry_date": pos.get("entry_date"),
        "exit_date": kf.today_ist(),
        "trend": pos.get("trend"),
        "spot": pos.get("spot"),
        "expiry": pos.get("expiry"),
        "strike": pos.get("strike"),
        "type": pos.get("type"),
        "tradingsymbol": pos.get("tradingsymbol"),
        "entry": entry,
        "qty": total_qty,
        "exit": float(exit_price),
        "pnl": total_pnl,
        "running_cap": round(running, 2),
        "peak": round(peak, 2),
        "drawdown": round((peak - running) / peak, 4) if peak > 0 and running < peak else 0.0,
        "risk_taken": round(float(pos.get("risk_fraction") or 0), 4),
        "return_on_cap": round(total_pnl / prev_cap, 4) if prev_cap else None,
        "is_new_peak": running > old_peak,
        "mode": pos.get("mode") or "paper",
        "entry_order_id": pos.get("order_id") or "",
        "exit_order_id": order.get("order_id") or "",
        "exit_reason": reason,
        "closed_ts": _now_iso(),
    }
    if with_partial:
        trade.update({"partial_tp": round(partial_exit, 4) if partial_exit > 0 else None,
                      "partial_qty": partial_qty, "partial_pnl": round(partial_pnl, 2),
                      "final_tp": float(exit_price), "final_qty": remain_qty, "final_pnl": round(final_pnl, 2)})
    state["trades"] = (state["trades"] + [trade])[-2000:]
    state["open_position"] = None
    state["peak_capital"] = _peak_from_path(state)
    state["last_flat_date"] = kf.today_ist()
    _record_order(state, order, "exit")
    _save(state)
    return trade


def _partial_close(*, order: dict[str, Any], exit_price: float, lots: int) -> dict[str, Any]:
    state = _load()
    pos = state.get("open_position")
    if not isinstance(pos, dict):
        raise ValueError("No open GC option position")
    lot_size = max(1, int(pos.get("lot_size") or 65))
    sell_qty = max(1, int(lots)) * lot_size
    open_qty = int(pos.get("qty") or 0)
    if sell_qty >= open_qty:
        return _close(order=order, exit_price=exit_price, exit_reason="Partial TP (+15%)")
    pnl = calc_pnl(float(pos["entry"]), exit_price, sell_qty)
    remain = open_qty - sell_qty
    pos = {**pos, "qty": remain, "lots": max(1, remain // lot_size), "partial_booked": True,
           "partial_lots": int(lots), "final_lots": max(1, remain // lot_size),
           "original_qty": int(pos.get("original_qty") or open_qty), "partial_qty": sell_qty,
           "partial_exit_price": float(exit_price), "partial_pnl": round(pnl, 2),
           "partial_exit_date": kf.today_ist(), "partial_order_id": order.get("order_id") or ""}
    state["open_position"] = pos
    _record_order(state, order, "partial_tp")
    _save(state)
    return {"ok": True, "partial": True, "exit": float(exit_price), "qty": sell_qty, "pnl": round(pnl, 2),
            "remain_qty": remain, "open_position": pos}


def open_count() -> int:
    return 1 if get_open_position() else 0


# ── runner ────────────────────────────────────────────────────────────────────

def _option_atr_proxy(*, premium: float, spot: float) -> float:
    """NIFTY 15m ATR scaled by premium/spot — live stand-in for option-premium ATR."""
    try:
        df = _bars15()
        if df is None or len(df) < 20 or spot <= 0:
            return max(premium * 0.02, 1.0)
        prev = df["close"].shift(1)
        tr = pd.concat([(df["high"] - df["low"]).abs(), (df["high"] - prev).abs(),
                        (df["low"] - prev).abs()], axis=1).max(axis=1)
        a = float(tr.tail(10).mean())
        if a <= 0:
            return max(premium * 0.02, 1.0)
        return max(a * (premium / spot), premium * 0.01, 1.0)
    except Exception:
        return max(premium * 0.02, 1.0)


def _premium(pos_or_leg: dict[str, Any]) -> float:
    tsym = str(pos_or_leg.get("tradingsymbol") or "")
    if not tsym:
        try:
            tsym = kf.option_contract(str(pos_or_leg.get("type") or "CE"), float(pos_or_leg.get("strike") or 0),
                                      str(pos_or_leg.get("expiry") or ""))["tradingsymbol"]
        except Exception:
            return 0.0
    return kf.nfo_ltp(tsym)


def _sell_lots(pos: dict[str, Any], lots: int, premium: float, tag: str) -> dict[str, Any]:
    lot_size = max(1, int(pos.get("lot_size") or 65))
    tsym = str(pos.get("tradingsymbol") or "")
    if not tsym:
        tsym = kf.option_contract(str(pos.get("type") or "CE"), float(pos.get("strike") or 0),
                                  str(pos.get("expiry") or ""))["tradingsymbol"]
    return kf.place(MODEL, "SELL", tsym, max(1, int(lots)) * lot_size, premium, tag)


def panel() -> dict[str, Any]:
    sig = signal()
    state = _load()
    pos = state.get("open_position")
    mark = _premium(pos) if isinstance(pos, dict) else None
    return {
        **sig,
        "config": state["config"],
        "open_position": pos,
        "mark_premium": mark or None,
        "trades": list(reversed(state["trades"][-200:])),
        "orders": list(reversed(state["orders"][-50:])),
        "summary": _summary(state, mark=mark or None),
        "phase": phase(),
        "mode": kf.model_mode(MODEL),
        "exits": [
            f"Tranche 1 (50%): take profit +{BUY_PARTIAL_TP_PCT * 100:.0f}% premium",
            f"Tranche 2 (50%): ATR trail {BUY_ATR_MULTIPLIER}× after partial; final TP ~{sig.get('final_tp_pct', '—')}% (VIX-scaled)",
            f"Hard stop ₹{BUY_MAX_LOSS:,.0f} max loss",
            f"Square-off {EOD_SQUARE.strftime('%H:%M')} IST",
        ],
    }


def enter_from_signal(force: bool = False) -> dict[str, Any]:
    if get_open_position():
        return {"ok": False, "message": "Already in a position — flat first"}
    sig = signal()
    if not sig.get("ok"):
        return {"ok": False, "message": sig.get("message") or "Signal unavailable"}
    opt = str(sig.get("suggested_option_type") or "").upper()
    if opt not in ("CE", "PE"):
        return {"ok": False, "skip": True, "message": f"No CE/PE signal ({sig.get('signal')})"}
    if not force and not sig.get("allow_entry"):
        return {"ok": False, "skip": True, "message": sig.get("entry_blocked_reason") or "Entry not allowed"}
    today = kf.today_ist()
    if not force and _load().get("last_entry_date") == today:
        return {"ok": False, "skip": True, "message": f"Already entered today ({today})"}

    strike, expiry, spot = float(sig["atm_strike"]), str(sig.get("expiry") or ""), float(sig["spot"])
    if strike <= 0 or not expiry:
        return {"ok": False, "message": "Missing ATM strike / expiry"}
    try:
        contract = kf.option_contract(opt, strike, expiry)
    except Exception as exc:
        return {"ok": False, "message": f"Option contract not found: {exc}"}
    premium = kf.nfo_ltp(contract["tradingsymbol"])
    if premium <= 0:
        return {"ok": False, "message": f"No premium for {contract['tradingsymbol']}"}

    cfg = get_config()
    lot_size = int(cfg.get("lot_size") or 65)
    risk_frac = float(sig["risk_fraction"])
    capital = curr_capital()
    sized = compute_quantity(capital=capital, risk_fraction=risk_frac, premium=premium, lot_size=lot_size)
    lots, qty = int(sized["lots"]), int(sized["quantity"])
    if lots < 1:
        return {"ok": False, "sizing": sized,
                "message": f"GC qty too small (risk {risk_frac:.0%} of ₹{capital:,.0f} vs premium ₹{premium:.2f} × {lot_size})"}

    order = kf.place(MODEL, "BUY", contract["tradingsymbol"], qty, premium, "enter")
    if not order.get("ok"):
        return order
    fill = float(order["fill_price"])
    partial_lots = 0 if lots < 2 else lots // 2
    atr_v = _option_atr_proxy(premium=fill, spot=spot)
    final_tp_pct = float(sig.get("final_tp_pct") or 0) / 100.0 or _vix_final_tp(float(sig.get("vix") or DEFAULT_VIX))
    state = _load()
    if state.get("open_position"):
        return {"ok": False, "message": "Position appeared while ordering — check the book"}
    pos = {
        "id": f"GC-{uuid.uuid4().hex[:8].upper()}",
        "entry_ts": _now_iso(),
        "trend": "LONG" if opt == "CE" else "SHORT",
        "type": opt,
        "spot": spot,
        "strike": strike,
        "expiry": expiry,
        "tradingsymbol": contract["tradingsymbol"],
        "entry": fill,
        "qty": qty,
        "lots": lots,
        "lot_size": lot_size,
        "risk_fraction": risk_frac,
        "risk_budget": sized["risk_budget"],
        "entry_date": today,
        "mode": order.get("mode") or "paper",
        "order_id": order.get("order_id") or "",
        "partial_booked": False,
        "partial_lots": partial_lots,
        "final_lots": lots - partial_lots,
        "original_qty": qty,
        "partial_tp_price": round(fill * (1 + BUY_PARTIAL_TP_PCT), 4),
        "final_tp_price": round(fill * (1 + final_tp_pct), 4),
        "highest_premium": fill,
        "trailing_stop": round(fill - BUY_ATR_MULTIPLIER * atr_v, 4),
        "atr_at_entry": round(atr_v, 4),
    }
    state["open_position"] = pos
    state["last_entry_date"] = today
    _record_order(state, order, "enter")
    _save(state)
    return {"ok": True, "action": "enter", "order": order, "open_position": pos, "sizing": sized}


def flat_position(exit_reason: str = "Flat") -> dict[str, Any]:
    pos = get_open_position()
    if not pos:
        return {"ok": False, "message": "No open position to flatten"}
    premium = _premium(pos) or float(pos.get("entry") or 0)
    order = _sell_lots(pos, max(1, int(pos.get("lots") or 1)), premium, "flat")
    if not order.get("ok"):
        return order
    trade = _close(order=order, exit_price=float(order["fill_price"]), exit_reason=exit_reason)
    return {"ok": True, "action": "flat", "order": order, "closed": trade}


def manage_open_exits() -> dict[str, Any]:
    pos = get_open_position()
    if not pos:
        return {"ok": True, "phase": "flat"}
    now_t = kf.now_ist().time().replace(tzinfo=None)
    stamp = now_t.strftime("%H:%M:%S")
    premium = _premium(pos)
    if premium <= 0:
        premium = float(pos.get("highest_premium") or pos.get("entry") or 0)
    spot = kf.nifty_spot() or float(pos.get("spot") or 0)
    entry = float(pos.get("entry") or 0)
    qty = int(pos.get("qty") or 0)
    lots = max(1, int(pos.get("lots") or 1))

    highest = max(float(pos.get("highest_premium") or entry), premium)
    atr_v = _option_atr_proxy(premium=premium, spot=spot or 1.0)
    trailing = max(float(pos.get("trailing_stop") or 0), highest - BUY_ATR_MULTIPLIER * atr_v)
    pos = _patch_open({"highest_premium": round(highest, 4), "trailing_stop": round(trailing, 4),
                       "last_mark": round(premium, 4), "last_atr": round(atr_v, 4)}) or pos

    partial_booked = bool(pos.get("partial_booked"))
    partial_lots = int(pos.get("partial_lots") or 0)
    partial_tp = float(pos.get("partial_tp_price") or 0)
    final_tp = float(pos.get("final_tp_price") or 0)

    if not partial_booked and partial_lots >= 1 and premium >= partial_tp:
        order = _sell_lots(pos, partial_lots, premium, "partial_tp")
        if not order.get("ok"):
            return {"ok": False, "phase": "partial_tp_failed", "message": order.get("message"), "now_ist": stamp}
        res = _partial_close(order=order, exit_price=float(order["fill_price"]), lots=partial_lots)
        return {"ok": True, "phase": "partial_tp", "now_ist": stamp, "actions": [res]}

    exit_reason, exit_price = None, premium
    if now_t >= EOD_SQUARE:
        exit_reason = "EOD Square-Off"
    elif partial_booked and premium <= trailing:
        exit_reason = "ATR Trailing Stop"
    elif final_tp > 0 and premium >= final_tp:
        exit_reason, exit_price = "Final TP", final_tp
    elif calc_pnl(entry, premium, qty) <= -BUY_MAX_LOSS:
        exit_reason = "Hard Stop (15k)"
        if qty > 0:
            exit_price = entry - BUY_MAX_LOSS / qty

    if exit_reason:
        order = _sell_lots(pos, lots, premium, "exit")
        if not order.get("ok"):
            return {"ok": False, "phase": "exit_failed", "message": order.get("message"), "now_ist": stamp}
        fill = float(order["fill_price"])
        if order.get("mode") == "paper" and exit_reason in ("Hard Stop (15k)", "Final TP"):
            fill = max(fill, float(exit_price))
        trade = _close(order=order, exit_price=fill, exit_reason=exit_reason)
        return {"ok": True, "phase": "exit", "message": f"{exit_reason} @ {fill}", "now_ist": stamp, "closed": trade}

    return {"ok": True, "phase": "hold", "now_ist": stamp,
            "message": f"Holding {'T2' if partial_booked else 'full'} · mark {premium:.2f} · trail {trailing:.2f} · "
                       f"TP1 {partial_tp:.2f} · TP2 {final_tp:.2f}"}


def daily_tick() -> dict[str, Any]:
    """Open → manage exits. Flat + auto_daily + CE/PE before 15:00 + not entered today → Enter."""
    now = kf.now_ist()
    stamp = now.strftime("%H:%M:%S")
    if kf.model_mode(MODEL) == "off":
        return {"ok": True, "phase": "model_off", "now_ist": stamp}
    if now.weekday() >= 5:
        return {"ok": True, "phase": "weekend", "now_ist": stamp}
    if get_open_position():
        return manage_open_exits()
    t = now.time().replace(tzinfo=None)
    if not get_config().get("auto_daily"):
        return {"ok": True, "phase": "idle_auto_off", "now_ist": stamp}
    if t >= ENTRY_CUTOFF:
        return {"ok": True, "phase": "past_cutoff", "now_ist": stamp}
    if t < dtime(9, 20):
        return {"ok": True, "phase": "pre_open", "now_ist": stamp}
    res = enter_from_signal(force=False)
    if res.get("skip"):
        return {"ok": True, "phase": "no_entry", "message": res.get("message"), "now_ist": stamp}
    return {"ok": bool(res.get("ok")), "phase": "entered" if res.get("ok") else "enter_failed",
            "message": res.get("message"), "now_ist": stamp, "actions": [res]}


def kill_exit() -> int:
    pos = get_open_position()
    if not pos:
        return 0
    tsym = str(pos.get("tradingsymbol") or "")
    if phase() == "live" and tsym:
        net = kf.broker_net_qty(tsym)
        if not net:
            kf.log(f"GC Options Buy kill-exit: broker shows no open {tsym} — nothing to close, book marked flat", level="WARNING")
            order = {"ok": True, "mode": "live", "order_id": "", "fill_price": _premium(pos) or float(pos["entry"]),
                     "tradingsymbol": tsym, "transaction_type": "", "quantity": int(pos["qty"])}
        else:
            order = kf.place(MODEL, "SELL" if net > 0 else "BUY", tsym, abs(net), _premium(pos) or float(pos["entry"]), "kill_exit")
    else:
        order = _sell_lots(pos, max(1, int(pos.get("lots") or 1)), _premium(pos) or float(pos["entry"]), "kill_exit")
    if not order.get("ok"):
        return 0
    _close(order=order, exit_price=float(order["fill_price"]), exit_reason="Kill-exit")
    return 1
