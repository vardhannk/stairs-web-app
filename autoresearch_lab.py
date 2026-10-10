"""Autoresearch Lab — paper-only forward tracking of the strangle strategies
found by the autoresearch backtest loop (strangle_autoresearch repo).

Each strategy sells a current-weekly CE and PE at fixed % OTM from the 09:20
NIFTY spot, entering on the Nth trading session after the previous expiry
(0 = the day after expiry, 1 = the session after that) and holding to expiry
with no stop. A "next" series strategy sells the following expiry instead and
buys it back on the current expiry day. Strategies with a vix_max skip the week
when the India VIX day open is above it. Sizing matches the backtest unless a
fixed lot count is set: every strategy compounds its own equity from ₹10L,
lots = floor(equity / margin_per_lot). Strategies can be added from the page.

Prices are Kite LTPs on the admin account. No order function is ever called.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import kite_feed as kf
import strangle_lab as sl

BOOK_KEY = "autoresearch_lab::book"
RESEARCH_KEY = "autoresearch_lab::research"
LABEL = "Autoresearch Lab"

STRATEGIES = [
    {"id": "thu25", "entry_offset": 1, "ce_pct": 2.5, "pe_pct": 2.5, "label": "Thu · 2.5% / 2.5% (recommended)"},
    {"id": "thu30", "entry_offset": 1, "ce_pct": 3.0, "pe_pct": 3.0, "label": "Thu · 3.0% / 3.0% (safer)"},
    {"id": "thu2520", "entry_offset": 1, "ce_pct": 2.5, "pe_pct": 2.0, "label": "Thu · CE 2.5% / PE 2.0% (search winner)"},
    {"id": "wed35", "entry_offset": 0, "ce_pct": 3.5, "pe_pct": 3.5, "label": "Wed · 3.5% / 3.5% (live rules)"},
    {"id": "fri1025", "entry_offset": 2, "ce_pct": 1.0, "pe_pct": 2.5, "label": "Fri · CE 1.0% / PE 2.5% (DD 12% pick*)"},
    {"id": "thu1020v", "entry_offset": 1, "ce_pct": 1.0, "pe_pct": 2.0, "vix_max": 20,
     "label": "Thu · CE 1.0% / PE 2.0%, VIX ≤ 20 (DD 20% pick*)"},
    {"id": "wed1025v", "entry_offset": 0, "ce_pct": 1.0, "pe_pct": 2.5, "vix_max": 20,
     "label": "Wed · CE 1.0% / PE 2.5%, VIX ≤ 20 (DD 25% pick*)"},
    {"id": "thu30ve", "entry_offset": 1, "ce_pct": 3.0, "pe_pct": 3.0, "vix_max": 20, "avoid_events": True,
     "label": "Thu · 3.0% / 3.0%, VIX ≤ 20, no event weeks (2019–26 pick)"},
    {"id": "thu25ve", "entry_offset": 1, "ce_pct": 2.5, "pe_pct": 2.5, "vix_max": 20, "avoid_events": True,
     "label": "Thu · 2.5% / 2.5%, VIX ≤ 20, no event weeks (2019–26 higher CAGR)"},
]

_warned = {"no_token": None}

DEFAULT_CONFIG = {
    "capital": 1_000_000,
    "margin_per_lot": 190_000,
    "lot_size": 65,
    "start_date": "2026-10-14",
    "end_date": "2027-04-14",
    # Scheduled market-moving government events (Budget, election results). Strategies with
    # avoid_events skip any expiry week containing one and exit an open trade the session before.
    "events": [{"date": "2027-02-01", "name": "Union Budget 2027"}],
    # Strategies added from the Nifty Strangles page (paper only), and fixed lot counts by
    # strategy id; a strategy without one sizes as floor(equity / margin_per_lot).
    "custom": [],
    "sizing": {},
}
TAB_IDS = ("thu30ve", "thu25ve")
DAYS = {0: "Wed", 1: "Thu", 2: "Fri", 3: "Mon"}


def strategies(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return STRATEGIES + [{**c, "custom": True} for c in cfg.get("custom") or []]


def _load() -> dict[str, Any]:
    data = kf.A().kv_get(BOOK_KEY, None) or {}
    cfg = {**DEFAULT_CONFIG, **(data.get("config") or {})}
    books = data.get("books") or {}
    for s in strategies(cfg):
        books.setdefault(s["id"], {"open": None, "trades": [], "last_entry_date": None})
        books[s["id"]].setdefault("skips", [])
    return {"config": cfg, "books": books, "removed": data.get("removed") or []}


def _label(c: dict[str, Any]) -> str:
    parts = [f"{DAYS[c['entry_offset']]} · CE {c['ce_pct']:g}% / PE {c['pe_pct']:g}%",
             "next week" if c["series"] == "next" else "this week",
             f"VIX ≤ {c['vix_max']:g}" if c.get("vix_max") is not None else "no VIX filter"]
    if c.get("avoid_events"):
        parts.append("no event weeks")
    return " · ".join(parts)


def edit_strategy(body: dict[str, Any]) -> dict[str, Any]:
    """add / remove a custom strategy, or set any strategy's lots (None = auto)."""
    state = _load()
    cfg = state["config"]
    action = body.get("action")
    ids = {s["id"] for s in strategies(cfg)}
    if action == "add":
        try:
            off = int(body.get("entry_offset"))
            ce, pe = round(float(body.get("ce_pct")), 2), round(float(body.get("pe_pct")), 2)
            vix = body.get("vix_max")
            vix = None if vix in (None, "", "none") else round(float(vix), 1)
        except (TypeError, ValueError):
            raise ValueError("entry day, CE %, PE % and VIX must be numbers")
        if off not in DAYS or not (0.5 <= ce <= 10 and 0.5 <= pe <= 10):
            raise ValueError("entry day must be Wed/Thu/Fri/Mon and CE/PE between 0.5% and 10%")
        if vix is not None and not 10 <= vix <= 40:
            raise ValueError("VIX cap must be between 10 and 40")
        c = {"id": "c" + kf.now_ist().strftime("%y%m%d%H%M%S"), "entry_offset": off, "ce_pct": ce, "pe_pct": pe,
             "series": "next" if body.get("series") == "next" else "current",
             "vix_max": vix, "avoid_events": bool(body.get("avoid_events"))}
        if c["id"] in ids:
            raise ValueError("try again in a second")
        c["label"] = str(body.get("name") or "").strip()[:60] or _label(c)
        cfg["custom"] = list(cfg.get("custom") or []) + [c]
        ids.add(c["id"])
        body = {**body, "id": c["id"]}
    elif action == "remove":
        sid = body.get("id")
        keep = [c for c in cfg.get("custom") or [] if c["id"] != sid]
        if len(keep) == len(cfg.get("custom") or []):
            raise ValueError("only tabs you added can be removed")
        cfg["custom"] = keep
        state["removed"].append({"strategy": sid, "book": state["books"].pop(sid, None),
                                 "removed_at": kf.now_ist().isoformat(timespec="seconds")})
        cfg.get("sizing", {}).pop(sid, None)
        _save(state)
        return {"ok": True, "config": cfg}
    elif action != "lots":
        raise ValueError("action must be add | remove | lots")
    if "lots" in body:
        sid, lots = body.get("id"), body.get("lots")
        if sid not in ids:
            raise ValueError("unknown strategy")
        sizing = dict(cfg.get("sizing") or {})
        if lots in (None, "", "auto"):
            sizing.pop(sid, None)
        else:
            n = int(float(lots))
            if not 1 <= n <= 100:
                raise ValueError("lots must be 1–100, or Auto")
            sizing[sid] = n
        cfg["sizing"] = sizing
    _save(state)
    return {"ok": True, "config": cfg, "id": body.get("id")}


def lots_for(cfg: dict[str, Any], sid: str, equity: float) -> int:
    fixed = (cfg.get("sizing") or {}).get(sid)
    return int(fixed) if fixed else int(equity // float(cfg["margin_per_lot"]))


def _save(state: dict[str, Any]) -> None:
    kf.A().kv_set(BOOK_KEY, state)


def set_config(updates: dict[str, Any]) -> dict[str, Any]:
    state = _load()
    cfg = state["config"]
    for k in ("capital", "margin_per_lot", "lot_size"):
        if k in updates and updates[k] not in ("", None):
            cfg[k] = int(float(updates[k]))
    for k in ("start_date", "end_date"):
        if updates.get(k) and kf.parse_date(updates[k]):
            cfg[k] = str(updates[k])[:10]
    if isinstance(updates.get("events"), list):
        events = []
        for e in updates["events"]:
            d = kf.parse_date(str((e or {}).get("date") or ""))
            if d:
                events.append({"date": d.isoformat(), "name": str(e.get("name") or "").strip()[:80] or "Event"})
        cfg["events"] = sorted({e["date"]: e for e in events}.values(), key=lambda e: e["date"])
    _save(state)
    return cfg


def _effective(day: str) -> str:
    """A weekend event (e.g. a Saturday Budget) hits the market on the next weekday."""
    d = date.fromisoformat(day)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def events_between(cfg: dict[str, Any], start: str, end: str) -> list[dict[str, str]]:
    """Events whose market session falls within [start, end], with `date` set to that session."""
    out = []
    for e in cfg.get("events") or []:
        eff = _effective(e["date"])
        if start <= eff <= end:
            out.append({**e, "date": eff})
    return out


def _last_session_before(event_date: str, today: str) -> bool:
    """True when `today` is the final weekday before the event (holidays are not modelled)."""
    d = date.fromisoformat(today) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return today < event_date <= d.isoformat()


def _equity(cfg: dict[str, Any], book: dict[str, Any]) -> float:
    return float(cfg["capital"]) + sum(float(t.get("pnl") or 0) for t in book["trades"])


def session_offset(today: str, current_expiry: str) -> int | None:
    """Which trading session after the previous expiry `today` is (0-based).
    Weekly expiries fall on Tuesday, or the trading day before it on a
    holiday, so the previous expiry is the last session on/before the nominal
    Tuesday one week before this expiry's nominal Tuesday."""
    bars = kf.index_bars(kf.NIFTY_TOKEN, "day", 20, max_age_s=300)
    if bars.empty:
        return None
    sessions = sorted({d.strftime("%Y-%m-%d") for d in bars.index})
    if today not in sessions:
        sessions.append(today)
    cur = date.fromisoformat(current_expiry)
    nominal = cur + timedelta(days=(1 - cur.weekday()) % 7)
    cutoff = (nominal - timedelta(days=7)).isoformat()
    prior = [d for d in sessions if d <= cutoff]
    if not prior:
        return None
    after = [d for d in sessions if prior[-1] < d <= today]
    return len(after) - 1


def vix_open(today: str) -> float | None:
    """Today's India VIX day open — the backtest filters on the VIX open, not the 09:20 print."""
    try:
        bars = kf.index_bars(kf.VIX_TOKEN, "day", 5, max_age_s=300)
    except Exception:
        return None
    if bars.empty or bars.index[-1].strftime("%Y-%m-%d") != today:
        return None
    v = float(bars["open"].iloc[-1])
    return v if v > 0 else None


def enter_due(now: datetime) -> dict[str, Any]:
    today = now.strftime("%Y-%m-%d")
    state = _load()
    cfg = state["config"]
    if not (cfg["start_date"] <= today <= cfg["end_date"]):
        return {"ok": True, "phase": "no_entry"}
    waiting = [s for s in strategies(cfg) if not state["books"][s["id"]]["open"]
               and state["books"][s["id"]].get("last_entry_date") != today]
    if not waiting:
        return {"ok": True, "phase": "hold"}
    if not kf.connected():
        if _warned["no_token"] == today:
            return {"ok": True, "phase": "hold"}
        _warned["no_token"] = today
        return {"ok": False, "phase": "no_token", "message": "Zerodha not connected — Autoresearch Lab entry skipped"}
    exps = sl.upcoming_expiries(2)
    expiry = exps[0]
    if expiry <= today:
        return {"ok": True, "phase": "no_entry"}
    offset = session_offset(today, expiry)
    due = [s for s in waiting if s["entry_offset"] == offset]
    if not due:
        return {"ok": True, "phase": "no_entry"}
    spot = kf.nifty_spot()
    if spot <= 0:
        return {"ok": False, "phase": "no_spot", "message": "Autoresearch Lab: could not read NIFTY spot"}
    opened, failed, skipped = [], [], []
    vix = None
    for s in due:
        book = state["books"][s["id"]]
        if s.get("avoid_events"):
            hit = events_between(cfg, today, expiry)
            if hit:
                book["skips"].append({"date": today, "reason": "event", "event": hit[0]["name"],
                                      "event_date": hit[0]["date"], "expiry": expiry})
                book["last_entry_date"] = today
                skipped.append(s["id"])
                continue
        if s.get("vix_max") is not None:
            if vix is None:
                vix = vix_open(today)
            if vix is None:
                failed.append(f"{s['id']}: could not read today's India VIX open")
                continue
            if vix > s["vix_max"]:
                book["skips"].append({"date": today, "reason": "vix", "vix": round(vix, 2), "expiry": expiry})
                book["last_entry_date"] = today
                skipped.append(s["id"])
                continue
        lots = lots_for(cfg, s["id"], _equity(cfg, book))
        sell_exp = exps[1] if s.get("series") == "next" and len(exps) > 1 else expiry
        if s.get("series") == "next" and sell_exp == expiry:
            failed.append(f"{s['id']}: next-week expiry unavailable")
            continue
        if lots <= 0:
            failed.append(f"{s['id']}: equity below one lot of margin")
            continue
        try:
            legs = {}
            for key, opt, side, pct in (("ce", "CE", "+", s["ce_pct"]), ("pe", "PE", "-", s["pe_pct"])):
                c = sl._contract(opt, sl.otm_strike(spot, pct, side), sell_exp)
                px = kf.nfo_ltp(c["tradingsymbol"])
                if px <= 0:
                    raise RuntimeError(f"no LTP for {c['tradingsymbol']}")
                legs[key] = {"tradingsymbol": c["tradingsymbol"], "strike": int(c["strike"]),
                             "option_type": opt, "entry_price": round(px, 2), "exit_price": None}
        except Exception as e:
            failed.append(f"{s['id']}: {e}")
            continue
        book["open"] = {"variant": s["id"], "entry_date": today, "entry_time": now.strftime("%H:%M"),
                        "spot": round(spot, 2), "expiry": sell_exp, "exit_on": expiry, "lots": lots,
                        "qty": lots * int(cfg["lot_size"]), "status": "OPEN", "legs": legs}
        book["last_entry_date"] = today
        opened.append(s["id"])
    _save(state)
    if opened:
        kf.log(f"{LABEL}: opened {', '.join(opened)} @ spot {spot:.2f}", details={"failed": failed})
    if skipped:
        kf.log(f"{LABEL}: skipped {', '.join(skipped)} (VIX cap or event week)", details={"vix": vix})
    if failed:
        kf.log(f"{LABEL}: entry failed for {'; '.join(failed)}", level="WARNING")
    return {"ok": not failed, "phase": "entered" if opened else "no_entry", "opened": opened,
            "skipped": skipped, "failed": failed}


def exit_due(now: datetime) -> dict[str, Any]:
    today = now.strftime("%Y-%m-%d")
    hhmm = now.hour * 100 + now.minute
    state = _load()
    cfg = state["config"]
    avoid = {s["id"] for s in strategies(cfg) if s.get("avoid_events")}
    closed = []
    for sid, book in state["books"].items():
        t = book.get("open")
        if not t:
            continue
        ev = events_between(cfg, t["entry_date"], t["exit_on"]) if sid in avoid and t["exit_on"] != today else []
        due_ev = [e for e in ev if _last_session_before(e["date"], today) or e["date"] <= today]
        if due_ev and (1520 <= hhmm <= 1529 or (due_ev[0]["date"] <= today and 915 <= hhmm <= 1529)):
            px = {k: kf.nfo_ltp(l["tradingsymbol"]) for k, l in t["legs"].items()}
            if not all(p > 0 for p in px.values()):
                continue
            prices, reason = px, f"event_exit: {due_ev[0]['name']}"
        elif t["exit_on"] == today and 1531 <= hhmm <= 1538:
            px = {k: kf.nfo_ltp(l["tradingsymbol"]) for k, l in t["legs"].items()}
            if not all(p > 0 for p in px.values()):
                continue
            prices, reason = px, "expiry_squareoff"
        elif t["exit_on"] < today and t["expiry"] == t["exit_on"]:
            prices, reason = sl._settle_expired(t), "expiry_settlement"
            if prices is None:
                continue
        elif t["exit_on"] < today:
            px = {k: kf.nfo_ltp(l["tradingsymbol"]) for k, l in t["legs"].items()}
            if not all(p > 0 for p in px.values()):
                continue
            prices, reason = px, "late_squareoff"
        else:
            continue
        sl._close_trade(t, prices, reason, today)
        book["trades"].append(t)
        book["open"] = None
        closed.append(sid)
    if closed:
        _save(state)
        kf.log(f"{LABEL}: closed {', '.join(closed)}")
    return {"ok": True, "phase": "exited" if closed else "hold", "closed": closed}


def tick(now: datetime | None = None) -> dict[str, Any]:
    """Called every ~60s from the admin book loop (IST weekdays 09:00–15:59)."""
    now = now or kf.now_ist()
    res = exit_due(now)
    hhmm = now.hour * 100 + now.minute
    if 920 <= hhmm <= 935:
        res = enter_due(now)
    return res


def panel(with_marks: bool = True) -> dict[str, Any]:
    state = _load()
    cfg = state["config"]
    out = []
    for s in strategies(cfg):
        book = state["books"][s["id"]]
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
        summary = sl._summary(book["trades"], float(cfg["capital"]))
        summary["equity"] = round(_equity(cfg, book), 2)
        out.append({**s, "series": s.get("series", "current"), "tab": bool(s.get("custom")) or s["id"] in TAB_IDS,
                    "lots_fixed": (cfg.get("sizing") or {}).get(s["id"]),
                    "next_lots": lots_for(cfg, s["id"], summary["equity"]),
                    "open": open_view, "trades": book["trades"], "skips": book["skips"], "summary": summary})
    return {"ok": True, "config": cfg, "strategies": out, "research": kf.A().kv_get(RESEARCH_KEY, None)}
