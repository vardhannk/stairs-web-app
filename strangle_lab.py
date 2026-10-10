"""Strangle Lab — paper-only forward tracking of Nifty short-strangle variants.

Every variant follows the Nifty Strangle 3.5% rules (SELL CE at spot+X%, SELL
PE at spot-X%, Wednesday 09:20 entry, square-off at 15:38 on the Tuesday
expiry of the current weekly series, no SL/target) and differs only in the OTM
% and which expiry series is sold:

  current — the nearest weekly expiry (held to that expiry)
  next    — the expiry after that, bought back on the same Tuesday as
            'current' (same 6-day hold, about a week of life left)

Prices are Kite LTPs read on the admin account. This module never places
orders: it has no import of, or call to, any order function.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import kite_feed as kf

BOOK_KEY = "strangle_lab::book"
LABEL = "Strangle Lab"

VARIANTS = [
    {"id": "c35", "pct": 3.5, "series": "current", "label": "3.5% · this week (baseline)"},
    {"id": "c30", "pct": 3.0, "series": "current", "label": "3.0% · this week"},
    {"id": "c25", "pct": 2.5, "series": "current", "label": "2.5% · this week"},
    {"id": "n35", "pct": 3.5, "series": "next", "label": "3.5% · next week"},
    {"id": "n30", "pct": 3.0, "series": "next", "label": "3.0% · next week"},
    {"id": "n25", "pct": 2.5, "series": "next", "label": "2.5% · next week"},
]
VARIANT_BY_ID = {v["id"]: v for v in VARIANTS}

DEFAULT_CONFIG = {
    "capital": 1_000_000,
    "lots": 10,
    "lot_size": 65,
    "start_date": "2026-10-14",
    "end_date": "2027-04-14",   # last Wednesday entry allowed; open trades still exit after it
}

STRIKE_STEP = 50

# Zerodha F&O options charges (2026). Brokerage is flat per executed order.
OPT_CHARGES = {
    "brokerage_per_order": 20.0,
    "stt_sell":            0.0015,     # on sell-side premium
    "txn_charge":          0.0003553,  # NSE, on premium turnover
    "sebi_charge":         0.0000010,
    "gst":                 0.18,       # on brokerage + txn + SEBI
    "stamp_buy":           0.00003,    # on buy-side premium
}


# ── storage ─────────────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    data = kf.A().kv_get(BOOK_KEY, None) or {}
    cfg = {**DEFAULT_CONFIG, **(data.get("config") or {})}
    variants = data.get("variants") or {}
    for v in VARIANTS:
        variants.setdefault(v["id"], {"open": None, "trades": []})
    return {"config": cfg, "variants": variants, "last_entry_date": data.get("last_entry_date")}


def _save(state: dict[str, Any]) -> None:
    kf.A().kv_set(BOOK_KEY, state)


def set_config(updates: dict[str, Any]) -> dict[str, Any]:
    state = _load()
    cfg = state["config"]
    for k in ("capital", "lots", "lot_size"):
        if k in updates and updates[k] not in ("", None):
            cfg[k] = int(float(updates[k]))
    for k in ("start_date", "end_date"):
        if updates.get(k) and kf.parse_date(updates[k]):
            cfg[k] = str(updates[k])[:10]
    _save(state)
    return cfg


# ── market helpers ──────────────────────────────────────────────────────────

def _with_owner_ctx(fn):
    a = kf.A()
    prev = getattr(a._kite_ctx, "user_id", None)
    a._kite_ctx.user_id = kf.owner_user_id()
    try:
        return fn(a)
    finally:
        a._kite_ctx.user_id = prev


def upcoming_expiries(n: int = 2) -> list[str]:
    """The next n NIFTY option expiries from the live instrument list, so a
    holiday-shifted expiry (e.g. Monday) is picked up correctly."""
    def _get(a):
        today = date.today()
        exps = sorted({r.get("expiry") for r in a.get_nfo_instruments()
                       if r.get("name") == "NIFTY" and r.get("segment") == "NFO-OPT"
                       and r.get("expiry") and r.get("expiry") >= today})
        return [e.isoformat() for e in exps[:n]]
    return _with_owner_ctx(_get)


def otm_strike(spot: float, pct: float, side: str) -> int:
    raw = spot * (1 + pct / 100.0) if side == "+" else spot * (1 - pct / 100.0)
    return int(round(raw / STRIKE_STEP) * STRIKE_STEP)


def _contract(opt_type: str, strike: int, expiry: str) -> dict[str, Any]:
    """Exact strike if listed, else the nearest listed 100-pt strike further OTM."""
    try:
        return kf.option_contract(opt_type, strike, expiry)
    except Exception:
        alt = (strike // 100 + 1) * 100 if opt_type == "CE" else (strike // 100) * 100
        if alt == strike:
            raise
        return kf.option_contract(opt_type, alt, expiry)


def charges(sell_prem: float, buy_prem: float, qty: int) -> float:
    r = OPT_CHARGES
    sell_val, buy_val = sell_prem * qty, buy_prem * qty
    brokerage = 2 * r["brokerage_per_order"]
    txn = r["txn_charge"] * (sell_val + buy_val)
    sebi = r["sebi_charge"] * (sell_val + buy_val)
    gst = r["gst"] * (brokerage + txn + sebi)
    return brokerage + r["stt_sell"] * sell_val + txn + sebi + gst + r["stamp_buy"] * buy_val


def _close_trade(trade: dict[str, Any], prices: dict[str, float], reason: str, when: str) -> None:
    qty = int(trade["qty"])
    gross = cost = 0.0
    for key, leg in trade["legs"].items():
        xp = float(prices[key])
        leg["exit_price"] = round(xp, 2)
        gross += (float(leg["entry_price"]) - xp) * qty
        cost += charges(float(leg["entry_price"]), xp, qty)
    trade.update({"status": "CLOSED", "exit_date": when, "exit_reason": reason,
                  "gross_pnl": round(gross, 2), "charges": round(cost, 2), "pnl": round(gross - cost, 2)})


# ── scheduler actions ───────────────────────────────────────────────────────

def _in_tracking_window(cfg: dict[str, Any], today: str) -> bool:
    return cfg["start_date"] <= today <= cfg["end_date"]


def enter_all(now: datetime | None = None) -> dict[str, Any]:
    """Open every variant that has no open trade. Safe to call repeatedly."""
    now = now or kf.now_ist()
    today = now.strftime("%Y-%m-%d")
    state = _load()
    cfg = state["config"]
    if not kf.connected():
        return {"ok": False, "phase": "no_token", "message": "Zerodha not connected — Strangle Lab entry skipped"}
    spot = kf.nifty_spot()
    if spot <= 0:
        return {"ok": False, "phase": "no_spot", "message": "Strangle Lab: could not read NIFTY spot"}
    exps = upcoming_expiries(2)
    if len(exps) < 2:
        return {"ok": False, "phase": "no_expiry", "message": "Strangle Lab: expiries unavailable"}
    qty = int(cfg["lots"]) * int(cfg["lot_size"])
    opened, failed = [], []
    for v in VARIANTS:
        book = state["variants"][v["id"]]
        if book.get("open"):
            continue
        expiry = exps[0] if v["series"] == "current" else exps[1]
        try:
            legs = {}
            for key, opt, side in (("ce", "CE", "+"), ("pe", "PE", "-")):
                c = _contract(opt, otm_strike(spot, v["pct"], side), expiry)
                px = kf.nfo_ltp(c["tradingsymbol"])
                if px <= 0:
                    raise RuntimeError(f"no LTP for {c['tradingsymbol']}")
                legs[key] = {"tradingsymbol": c["tradingsymbol"], "strike": int(c["strike"]),
                             "option_type": opt, "entry_price": round(px, 2), "exit_price": None}
        except Exception as e:
            failed.append(f"{v['id']}: {e}")
            continue
        book["open"] = {"variant": v["id"], "entry_date": today, "entry_time": now.strftime("%H:%M"),
                        "spot": round(spot, 2), "expiry": expiry, "exit_on": exps[0], "qty": qty,
                        "status": "OPEN", "legs": legs}
        opened.append(v["id"])
    if opened:
        state["last_entry_date"] = today
    _save(state)
    if opened:
        kf.log(f"{LABEL}: opened {', '.join(opened)} @ spot {spot:.2f}", details={"failed": failed})
    if failed:
        kf.log(f"{LABEL}: entry failed for {'; '.join(failed)}", level="WARNING")
    return {"ok": not failed, "phase": "entered" if opened else "no_entry", "opened": opened, "failed": failed}


def _settle_expired(trade: dict[str, Any]) -> dict[str, float] | None:
    """Intrinsic value at the expiry-day NIFTY close, for a current-series trade
    whose exit window was missed after the contract has already expired."""
    try:
        bars = kf.index_bars(kf.NIFTY_TOKEN, "day", 20, max_age_s=600)
        close = float(bars[bars.index.strftime("%Y-%m-%d") == trade["exit_on"]]["close"].iloc[-1])
    except Exception:
        return None
    ce, pe = trade["legs"]["ce"], trade["legs"]["pe"]
    return {"ce": max(close - ce["strike"], 0.0), "pe": max(pe["strike"] - close, 0.0)}


def exit_due(now: datetime | None = None, force_window: bool = False) -> dict[str, Any]:
    """Square off every open trade whose exit day has arrived (15:31–15:38 on
    exit_on) or was missed."""
    now = now or kf.now_ist()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.hour * 100 + now.minute
    state = _load()
    closed, waiting = [], []
    for vid, book in state["variants"].items():
        t = book.get("open")
        if not t:
            continue
        due_today = t["exit_on"] == today and (force_window or 1531 <= hhmm <= 1538)
        overdue = t["exit_on"] < today
        if not (due_today or overdue):
            continue
        prices = None
        if overdue and t["expiry"] <= today and t["expiry"] == t["exit_on"]:
            prices = _settle_expired(t)
            reason = "expiry_settlement"
        else:
            px = {k: kf.nfo_ltp(l["tradingsymbol"]) for k, l in t["legs"].items()}
            if all(p > 0 for p in px.values()):
                prices, reason = px, ("tuesday_squareoff" if due_today else "late_squareoff")
        if prices is None:
            waiting.append(vid)
            continue
        _close_trade(t, prices, reason, today)
        book["trades"].append(t)
        book["open"] = None
        closed.append(vid)
    if closed:
        _save(state)
        kf.log(f"{LABEL}: closed {', '.join(closed)}")
    return {"ok": True, "phase": "exited" if closed else "hold", "closed": closed, "waiting": waiting}


def tick(now: datetime | None = None) -> dict[str, Any]:
    """Called every ~60s from the admin book loop (IST weekdays 09:00–15:59)."""
    now = now or kf.now_ist()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.hour * 100 + now.minute
    state = _load()
    res = exit_due(now)
    if now.weekday() == 2 and 920 <= hhmm <= 935 and _in_tracking_window(state["config"], today):
        state = _load()
        if any(not b.get("open") for b in state["variants"].values()):
            res = enter_all(now)
    return res


# ── view ────────────────────────────────────────────────────────────────────

def _summary(trades: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    pnls = [float(t.get("pnl") or 0) for t in trades]
    run = peak = float(capital)
    max_dd = 0.0
    for p in pnls:
        run += p
        peak = max(peak, run)
        max_dd = min(max_dd, (run - peak) / peak if peak else 0.0)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    prem = [sum(float(l["entry_price"]) for l in t["legs"].values()) for t in trades]
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "net_pnl": round(sum(pnls), 2), "return_pct": round(sum(pnls) / capital * 100, 2) if capital else 0,
        "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        "worst": round(min(pnls), 2) if pnls else 0, "best": round(max(pnls), 2) if pnls else 0,
        "max_dd_pct": round(max_dd * 100, 2),
        "avg_premium": round(sum(prem) / len(prem), 2) if prem else 0,
    }


def panel(with_marks: bool = True) -> dict[str, Any]:
    state = _load()
    cfg = state["config"]
    out = []
    for v in VARIANTS:
        book = state["variants"][v["id"]]
        t = book.get("open")
        open_view = None
        if t:
            open_view = dict(t)
            if with_marks and kf.connected():
                try:
                    marks = {k: kf.nfo_ltp(l["tradingsymbol"]) for k, l in t["legs"].items()}
                    if all(m > 0 for m in marks.values()):
                        open_view["marks"] = marks
                        open_view["open_pnl"] = round(sum((float(l["entry_price"]) - marks[k]) * int(t["qty"])
                                                          for k, l in t["legs"].items()), 2)
                except Exception:
                    pass
        out.append({**v, "open": open_view, "trades": book["trades"],
                    "summary": _summary(book["trades"], float(cfg["capital"]))})
    return {"ok": True, "config": cfg, "variants": out,
            "backtest": kf.A().kv_get("strangle_lab::backtest", None)}
