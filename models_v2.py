"""
STAIRS — models_v2.py
=====================

Clean rewrite of trade handlers for Options Buy (OB), Options AIT (AIT),
Nifty EXP (NE), and their workstation counterparts (OBW, AITW).

Futures handler remains in app.py (unchanged — works correctly).

Architecture principles:
  1. SINGLE SOURCE OF TRUTH — the trades list. positions dict is UI cache only.
  2. ONE HANDLER PER SIGNAL — no fallback paths, no duplicated invocations.
  3. LIVE SPOT — scheduler-driven actions fetch live NIFTY spot at action time.
  4. TOKEN GUARD — refuses to write trades if Zerodha disconnected.
  5. ATOMIC WRITES — close + open happens as a single state mutation.
  6. IDEMPOTENT — scheduler actions guarded by last_action_date marker.

Storage keys (SQLite kv table):
  Main strategy (ATR 17 / 0.9):
    strategy_bundle::options_buy
    strategy_bundle::options_ait
    strategy_bundle::nifty_exp
  Workstation strategy (ATR 2 / 2.7):
    strategy_bundle::ob_workstation
    strategy_bundle::ait_workstation
  Cache (positions dict — UI display only, never read by handlers):
    dry_run_module_positions
    workstation_positions

Public entry points (called from app.py):
  Main webhook signal:
    handle_main_signal(signal, spot, signal_time, app_module)
  Workstation webhook signal:
    handle_workstation_signal(signal, spot, signal_time, app_module)
  Scheduler tick (called every 60s from app.py's position_sync_job):
    scheduler_tick(now_ist, app_module)

The `app_module` parameter is the imported app.py module — used to access:
  app_module.kv_get, kv_set, log_automation, send_telegram, get_access_token,
  get_kite, get_nearest_nifty_weekly_expiry, get_next_nifty_weekly_expiry_after,
  master_state, CURRENT_NIFTY_LOT_SIZE, now_utc_iso, traceback, math
"""

from datetime import date as _date
import math
import traceback


# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — strategy rules locked in as constants
# ════════════════════════════════════════════════════════════════════════════

NIFTY_LOT_SIZE = 65
STRIKE_GAP     = 100
TXN_COST_RATE  = 0.004   # 0.4% per leg

# NSE's CAS (Closing Auction Session) change extended the trading/close window
# to ~3:40 PM. Brand-new positions this close to square-off leave almost no
# room to manage risk, so no NEW entry is allowed at/after this time. Existing
# open positions can still be closed/squared-off as normal — this only blocks
# opening fresh trades.
ENTRY_CUTOFF_HHMM = 1515


def _past_entry_cutoff(now_ist=None):
    """True if it's at/after 3:15 PM IST — no new entries allowed past this."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    if now_ist is None:
        _IST = _tz(_td(hours=5, minutes=30))
        now_ist = _dt.now(_IST)
    return (now_ist.hour * 100 + now_ist.minute) >= ENTRY_CUTOFF_HHMM

# Per-model configuration
MODEL_CONFIG = {
    "ob_workstation": {
        "label":         "OB Workstation",
        "storage":       "strategy_bundle::ob_workstation",
        "capital":       1_000_000,
        "risk_pct":      0.07,
        "channel":       "workstation",
        "type":          "directional_buy",
    },
    "ait_workstation": {
        "label":         "AIT Workstation",
        "storage":       "strategy_bundle::ait_workstation",
        "capital":       500_000,
        "risk_pct":      0.10,
        "channel":       "workstation",
        "type":          "credit_spread",
    },
    "nexp_workstation": {
        "label":         "NiftyEXP Workstation",
        "storage":       "strategy_bundle::nexp_workstation",
        "capital":       500_000,
        "risk_pct":      0.12,
        "channel":       "workstation",
        "type":          "credit_spread",
        "windowed":      True,
        "no_rollover":   True,
    },
    "nifty_strangle_w": {
        "label":         "Nifty Strangle 3.5%",
        "storage":       "strategy_bundle::nifty_strangle_w",
        "capital":       1_000_000,
        "risk_pct":      0.10,
        "channel":       "workstation",
        "type":          "short_strangle",
        "windowed":      True,
        "no_rollover":   True,
    },
}


# ════════════════════════════════════════════════════════════════════════════
# CORE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _today_iso():
    return _date.today().isoformat()


def fetch_live_nifty_spot(app):
    """Fetch live NIFTY 50 spot from Zerodha. Falls back to master_state if Zerodha fails."""
    try:
        kite = app.get_kite(require_token=True)
        q = kite.ltp(["NSE:NIFTY 50"])
        ltp = float(q["NSE:NIFTY 50"]["last_price"])
        if ltp > 0:
            return ltp
    except Exception as e:
        app.log_automation(f"fetch_live_nifty_spot ERROR — using master_state fallback: {e}", level="WARNING")
    return float(app.master_state.get("nifty_spot") or 0)


def round_to_100(value):
    """Round to nearest 100 for ATM strike calculation."""
    return int(round(value / 100.0) * 100)


def find_open_trade(trades):
    """Scan trades list for the latest OPEN trade. Returns (idx, trade) or (None, None).

    THIS IS THE SOURCE OF TRUTH for 'is there a position?'.
    Code MUST NOT read open-status from the positions dict.
    """
    for i in range(len(trades) - 1, -1, -1):
        t = trades[i]
        if isinstance(t, dict) and str(t.get("status", "")).upper() == "OPEN":
            return i, t
    return None, None


def find_all_open_trades(trades):
    """Returns list of (idx, trade) for ALL open trades. Used for orphan cleanup."""
    return [(i, t) for i, t in enumerate(trades)
            if isinstance(t, dict) and str(t.get("status", "")).upper() == "OPEN"]


_TXN = 0.004  # matches OBW_TXN / AITW_TXN / NEXPW_TXN in the frontend


def _mround(x, m):
    return round(x / m) * m if m else 0


def _trade_pnl(module_name, t):
    """Realised P&L of a CLOSED trade, matching the frontend equity math.
    Returns None if the trade is still open (no exit recorded)."""
    try:
        if module_name == "ob_workstation":
            ep = float(t.get("entry_price") or 0)
            xp = t.get("exit_price")
            qty = int(t.get("qty") or 0)
            if xp in ("", None) or not (ep and qty):
                return None
            xp = float(xp)
            return (xp - ep) * qty - _TXN * ep * qty - _TXN * xp * qty
        # spread models (AIT / NiftyEXP)
        atm_sell = float(t.get("atm_sell_price") or 0)
        otm_buy = float(t.get("otm_buy_price") or 0)
        atm_exit = t.get("atm_exit_price")
        otm_exit = t.get("otm_exit_price")
        qty = int(t.get("qty") or 0)
        if atm_exit in ("", None) or otm_exit in ("", None) or qty <= 0:
            return None
        atm_exit = float(atm_exit); otm_exit = float(otm_exit)
        return ((otm_exit - otm_buy) + (atm_sell - atm_exit)) * qty \
            - _TXN * qty * (atm_sell + otm_buy + atm_exit + otm_exit)
    except Exception:
        return None


def model_current_drawdown(module_name, trades, capital):
    """Running drawdown (<= 0 fraction) from the model's CLOSED trades — the same
    peak-to-current equity the page shows. Drives the risk multiplier on the next
    entry so position size shrinks in drawdown (mirrors the Excel sizing)."""
    run = float(capital or 0)
    peak = run
    for t in (trades or []):
        pnl = _trade_pnl(module_name, t)
        if pnl is None:
            continue
        run += pnl
        if run > peak:
            peak = run
    if peak <= 0:
        return 0.0
    return (run - peak) / peak


def compute_qty_directional(capital, risk_pct, premium, dd=0.0, tol=0.0, lot_size=NIFTY_LOT_SIZE):
    """Options Buy qty with drawdown-scaled risk (mirrors frontend obwCalcQty).
    dd > -10% -> full risk; -10%..-20% -> x0.6; <= -20% -> x0.2."""
    if premium <= 0:
        return 0
    mult = 1.0 if dd > -0.10 else (0.6 if dd > -0.20 else 0.2)
    adj_risk = capital * (risk_pct + risk_pct * tol) * mult
    lots = round(adj_risk / premium / lot_size) * lot_size
    if lots * premium > adj_risk:          # position cost overshoots budget -> drop a lot
        lots -= lot_size
    return max(int(lots), 0)


def compute_qty_spread(capital, risk_pct, dd=0.0, tol=0.0, lot_size=NIFTY_LOT_SIZE):
    """AIT/NiftyEXP spread qty with drawdown-scaled risk (mirrors frontend
    aitwCalcQty/nexpwCalcQty). dd > -15% -> full risk; <= -15% -> x0.6."""
    mult = 1.0 if dd > -0.15 else 0.6
    q = _mround(capital * (risk_pct * mult) * (1 + tol) / STRIKE_GAP, lot_size)
    return max(int(q), 0)


def safe_quote_directional(app, signal, spot, expiry):
    """Fetch quote for directional buy (CE for LONG, PE for SHORT). Returns dict."""
    try:
        return app.quote_option(signal, float(spot), expiry)
    except Exception as e:
        app.log_automation(f"safe_quote_directional fallback: {e}", level="WARNING")
        atm = round_to_100(spot)
        opt_type = "CE" if signal == "LONG" else "PE"
        return {
            "tradingsymbol": "",
            "option_type":   opt_type,
            "strike":        atm,
            "expiry":        expiry,
            "premium":       0.0,
        }


def safe_quote_spread(app, signal, spot, expiry):
    """Fetch quote for credit spread. Returns dict with ATM and OTM legs."""
    try:
        return app.get_nifty_spread_quote(signal, float(spot), expiry, STRIKE_GAP)
    except Exception as e:
        app.log_automation(f"safe_quote_spread fallback: {e}", level="WARNING")
        atm = round_to_100(spot)
        otm = atm - STRIKE_GAP if signal == "LONG" else atm + STRIKE_GAP
        opt_type = "PE" if signal == "LONG" else "CE"
        return {
            "atm_strike":        atm,
            "otm_strike":        otm,
            "option_type":       opt_type,
            "atm_tradingsymbol": "",
            "otm_tradingsymbol": "",
            "atm_sell_premium":  0.0,
            "otm_buy_premium":   0.0,
            "expiry":            expiry,
        }


def update_positions_cache(app, module_name, channel, trade=None, trade_index=None, qty=None):
    """Update positions dict (UI display cache). Handlers MUST NOT read from this dict for decisions.

    Pass trade=None to clear the position entry.
    """
    cache_key = "workstation_positions" if channel == "workstation" else "dry_run_module_positions"
    cache = app.kv_get(cache_key, {}) or {}
    if trade is None:
        cache.pop(module_name, None)
    else:
        if MODEL_CONFIG[module_name]["type"] == "credit_spread":
            cache[module_name] = {
                "status":      "OPEN",
                "signal":      trade.get("trend"),
                "trade_index": trade_index,
                "atm_symbol":  trade.get("atm_tradingsymbol"),
                "otm_symbol":  trade.get("otm_tradingsymbol"),
                "qty":         qty,
            }
        else:
            cache[module_name] = {
                "status":      "OPEN",
                "signal":      trade.get("trend"),
                "trade_index": trade_index,
                "symbol":      trade.get("tradingsymbol"),
                "qty":         qty,
            }
    app.kv_set(cache_key, cache)


# ════════════════════════════════════════════════════════════════════════════
# LIVE EXECUTION LAYER  (workstation models only; fully gated)
#
# Everything above is record-only and unchanged. These helpers translate a
# just-recorded paper trade into REAL Zerodha orders — but ONLY when app.py
# reports the model is live: global automation mode == LIVE AND the model is in
# app.get_live_enabled_models(). Otherwise every function here is a no-op, so
# the models stay paper until each is explicitly flipped live.
#
# Leg ordering (margin efficiency; avoids naked-short rejection):
#   Credit-spread ENTRY : BUY OTM hedge  -> wait fill -> SELL ATM short
#   Credit-spread EXIT  : BUY back ATM   -> wait fill -> SELL OTM hedge
#   Directional  ENTRY  : BUY option
#   Directional  EXIT   : SELL option
# Partial fill (one leg done, other not): HALT + Telegram alert, NO blind
# retry. The trade is tagged live_status='PARTIAL_HALT' for manual handling.
# ════════════════════════════════════════════════════════════════════════════

WORKSTATION_MODELS = ("ob_workstation", "ait_workstation", "nexp_workstation")


def _is_live_for(app, module_name):
    """True only if this workstation model should place REAL orders right now.
    Sole gate is per-model enablement (the go-live board) — no global switch."""
    if module_name not in WORKSTATION_MODELS:
        return False
    try:
        return module_name in app.get_live_enabled_models()
    except Exception as e:
        app.log_automation(f"{module_name}: _is_live_for failed, defaulting to PAPER: {e}", level="WARNING")
        return False


def _wait_for_fill(app, order_id, timeout_s=30, poll_s=2):
    """Poll order status until COMPLETE. Returns True if filled, False on
    timeout/reject/cancel."""
    import time as _t
    if not order_id:
        return False
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        try:
            kite = app.get_kite(require_token=True)
            for o in kite.orders():
                if str(o.get("order_id")) == str(order_id):
                    st = str(o.get("status", "")).upper()
                    if st == "COMPLETE":
                        return True
                    if st in ("REJECTED", "CANCELLED"):
                        return False
        except Exception as e:
            app.log_automation(f"_wait_for_fill poll error: {e}", level="WARNING")
        _t.sleep(poll_s)
    return False


def _available_equity_balance(app):
    """Best-effort read of free equity balance. Returns float or None."""
    try:
        kite = app.get_kite(require_token=True)
        m = kite.margins()
        seg = (m or {}).get("equity") or {}
        val = seg.get("net")
        if val is None:
            val = (seg.get("available") or {}).get("live_balance")
        return float(val) if val is not None else None
    except Exception as e:
        app.log_automation(f"margin read failed: {e}", level="WARNING")
        return None


def _preflight_margin_ok(app, label, required_estimate):
    """Pre-flight margin check. Blocks only on a CONFIRMED shortfall; does not
    block on API uncertainty (returns ok=True with a note)."""
    avail = _available_equity_balance(app)
    if avail is None:
        return True, "margin unreadable — proceeding (verify manually)"
    if avail <= 0:
        return True, f"margin reported 0 — proceeding (verify manually)"
    if avail < required_estimate:
        return False, f"shortfall: available ₹{avail:,.0f} < required ~₹{required_estimate:,.0f}"
    return True, f"OK: available ₹{avail:,.0f} >= required ~₹{required_estimate:,.0f}"


def _alert_partial(app, label, msg, trade):
    trade["live_status"] = "PARTIAL_HALT"
    app.log_automation(f"{label} LIVE PARTIAL-FILL HALT: {msg}", level="ERROR")
    try:
        app.send_telegram(
            f"🚨 <b>{label} PARTIAL FILL — HALTED</b>\n{msg}\n"
            f"⚠️ Manual intervention required. No automatic retry.")
    except Exception:
        pass


def _live_entry(app, module_name, trade, qty):
    """Place REAL entry orders for a just-recorded workstation trade.
    Mutates trade with live_status + order ids. Returns result dict."""
    config = MODEL_CONFIG[module_name]
    label  = config["label"]

    if config["type"] == "directional_buy":
        symbol = trade.get("tradingsymbol")
        est = float(trade.get("entry_price") or 0) * qty
        ok, note = _preflight_margin_ok(app, label, est)
        app.log_automation(f"{label} LIVE entry preflight — {note}", level="INFO")
        if not ok:
            trade["live_status"] = "MARGIN_HALT"
            try:
                app.send_telegram(f"⛔ <b>{label} LIVE ENTRY HALTED</b>\n{note}")
            except Exception:
                pass
            return {"ok": False, "live_status": "MARGIN_HALT", "note": note}
        r = app.place_live_order_with_retry("BUY", symbol, qty, reason=f"{module_name}_entry")
        filled = r.get("ok") and not r.get("dry_run")
        trade["live_entry_order_id"] = r.get("order_id")
        trade["live_status"] = "LIVE_OPEN" if filled else "ENTRY_FAILED"
        if filled:
            _wait_for_fill(app, r.get("order_id"))
        return {"ok": bool(filled), "live_status": trade["live_status"], "detail": r}

    # ---- credit spread: BUY OTM hedge FIRST, then SELL ATM short ----
    atm_sym = trade.get("atm_tradingsymbol")
    otm_sym = trade.get("otm_tradingsymbol")
    est = STRIKE_GAP * qty  # conservative max-loss margin estimate
    ok, note = _preflight_margin_ok(app, label, est)
    app.log_automation(f"{label} LIVE entry preflight — {note}", level="INFO")
    if not ok:
        trade["live_status"] = "MARGIN_HALT"
        try:
            app.send_telegram(f"⛔ <b>{label} LIVE ENTRY HALTED</b>\n{note}")
        except Exception:
            pass
        return {"ok": False, "live_status": "MARGIN_HALT", "note": note}

    # Leg 1 — BUY OTM hedge
    r_hedge = app.place_live_order_with_retry("BUY", otm_sym, qty, reason=f"{module_name}_entry_hedge")
    if not (r_hedge.get("ok") and not r_hedge.get("dry_run")):
        trade["live_status"] = "ENTRY_FAILED_HEDGE"
        return {"ok": False, "live_status": "ENTRY_FAILED_HEDGE", "detail": r_hedge}
    trade["live_hedge_order_id"] = r_hedge.get("order_id")
    if not _wait_for_fill(app, r_hedge.get("order_id")):
        _alert_partial(app, label, "hedge BUY placed but not confirmed filled — short leg NOT sent", trade)
        return {"ok": False, "live_status": "PARTIAL_HALT", "detail": "hedge unfilled"}

    # Leg 2 — SELL ATM short (hedge now covers margin)
    r_short = app.place_live_order_with_retry("SELL", atm_sym, qty, reason=f"{module_name}_entry_short")
    if not (r_short.get("ok") and not r_short.get("dry_run")):
        _alert_partial(app, label, "hedge FILLED but short SELL failed — you hold the long hedge only", trade)
        return {"ok": False, "live_status": "PARTIAL_HALT", "detail": r_short}
    trade["live_short_order_id"] = r_short.get("order_id")
    if not _wait_for_fill(app, r_short.get("order_id")):
        _alert_partial(app, label, "short SELL placed but not confirmed filled", trade)
        return {"ok": False, "live_status": "PARTIAL_HALT", "detail": "short unfilled"}

    trade["live_status"] = "LIVE_OPEN"
    return {"ok": True, "live_status": "LIVE_OPEN", "detail": {"hedge": r_hedge, "short": r_short}}


def _live_exit(app, module_name, trade, qty):
    """Place REAL exit orders for a just-closed workstation trade.
    Mutates trade with live_status. Returns result dict."""
    config = MODEL_CONFIG[module_name]
    label  = config["label"]

    if config["type"] == "directional_buy":
        symbol = trade.get("tradingsymbol")
        r = app.place_live_order_with_retry("SELL", symbol, qty, reason=f"{module_name}_exit")
        filled = r.get("ok") and not r.get("dry_run")
        trade["live_exit_order_id"] = r.get("order_id")
        trade["live_status"] = "LIVE_CLOSED" if filled else "EXIT_FAILED"
        if filled:
            _wait_for_fill(app, r.get("order_id"))
        return {"ok": bool(filled), "live_status": trade["live_status"], "detail": r}

    # ---- credit spread: BUY back ATM short FIRST, then SELL OTM hedge ----
    atm_sym = trade.get("atm_tradingsymbol")
    otm_sym = trade.get("otm_tradingsymbol")

    # Leg 1 — BUY back the ATM short
    r_cover = app.place_live_order_with_retry("BUY", atm_sym, qty, reason=f"{module_name}_exit_cover")
    if not (r_cover.get("ok") and not r_cover.get("dry_run")):
        trade["live_status"] = "EXIT_FAILED_COVER"
        return {"ok": False, "live_status": "EXIT_FAILED_COVER", "detail": r_cover}
    trade["live_cover_order_id"] = r_cover.get("order_id")
    if not _wait_for_fill(app, r_cover.get("order_id")):
        _alert_partial(app, label, "ATM buy-back placed but not confirmed filled — hedge NOT sold", trade)
        return {"ok": False, "live_status": "PARTIAL_HALT", "detail": "cover unfilled"}

    # Leg 2 — SELL the OTM hedge
    r_unhedge = app.place_live_order_with_retry("SELL", otm_sym, qty, reason=f"{module_name}_exit_unhedge")
    if not (r_unhedge.get("ok") and not r_unhedge.get("dry_run")):
        _alert_partial(app, label, "ATM covered but hedge SELL failed — you hold the long hedge only", trade)
        return {"ok": False, "live_status": "PARTIAL_HALT", "detail": r_unhedge}
    trade["live_unhedge_order_id"] = r_unhedge.get("order_id")
    if not _wait_for_fill(app, r_unhedge.get("order_id")):
        _alert_partial(app, label, "hedge SELL placed but not confirmed filled", trade)
        return {"ok": False, "live_status": "PARTIAL_HALT", "detail": "unhedge unfilled"}

    trade["live_status"] = "LIVE_CLOSED"
    return {"ok": True, "live_status": "LIVE_CLOSED", "detail": {"cover": r_cover, "unhedge": r_unhedge}}


# ════════════════════════════════════════════════════════════════════════════
# TRADE BUILDERS — produce trade dicts for each model type
# ════════════════════════════════════════════════════════════════════════════

def build_directional_trade(signal, spot, expiry, quote, qty, capital, risk_pct, signal_time, source="signal"):
    return {
        "date":              _today_iso(),
        "trend":             signal,
        "spot":              float(spot),
        "expiry":            expiry,
        "type":              (quote.get("contract") or {}).get("option_type") or quote.get("option_type"),
        "entry_price":       float(quote.get("premium") or 0.0),
        "exit_price":        "",
        "qty":               qty,
        "pnl":               "",
        "exit_date":         "",
        "capital":           capital,
        "risk_pct":          risk_pct,
        "return_pct":        "",
        "dd_pct":            "",
        "status":            "OPEN",
        "entry_signal_time": signal_time,
        "tradingsymbol":     (quote.get("contract") or {}).get("tradingsymbol") or quote.get("tradingsymbol"),
        "source":            source,
    }


def build_spread_trade(signal, spot, expiry, spread, qty, capital, risk_pct, signal_time, source="signal"):
    return {
        "date":              _today_iso(),
        "trend":             signal,
        "spot":              float(spot),
        "expiry":            expiry,
        "type":              spread.get("option_type"),
        "atm_strike":        spread.get("atm_strike"),
        "atm_sell_price":    float(spread.get("atm_sell_premium") or 0.0),
        "atm_exit_price":    "",
        "atm_tradingsymbol": spread.get("atm_tradingsymbol"),
        "otm_strike":        spread.get("otm_strike"),
        "otm_buy_price":     float(spread.get("otm_buy_premium") or 0.0),
        "otm_exit_price":    "",
        "otm_tradingsymbol": spread.get("otm_tradingsymbol"),
        "qty":               qty,
        "pnl":               "",
        "exit_date":         "",
        "capital":           capital,
        "risk_pct":          risk_pct,
        "return_pct":        "",
        "dd_pct":            "",
        "status":            "OPEN",
        "entry_signal_time": signal_time,
        "source":            source,
    }


# ════════════════════════════════════════════════════════════════════════════
# CORE HANDLER — used by all 5 models with different configs
# ════════════════════════════════════════════════════════════════════════════

def _process_model_signal(app, module_name, signal, spot, signal_time):
    """
    Universal handler for one model on a webhook signal.

    Algorithm:
      1. Token guard
      2. Load trades, find open trade by scanning list (NOT positions dict)
      3. Decide: no_action / flip / open_new
      4. Atomic write: build complete new trades list, write once
      5. Update positions cache (UI only)
    """
    config = MODEL_CONFIG[module_name]
    label  = config["label"]

    # ── 0. MODE GUARD — Off (standby) means no recording/trading at all ──
    try:
        if app.get_model_mode(module_name) == "off":
            app.log_automation(f"{label}: mode OFF (standby) — signal ignored, no tracking", level="INFO")
            return
    except Exception:
        pass
    signal = str(signal).upper()
    today  = _today_iso()

    # ── 1. TOKEN GUARD ────────────────────────────────────────────────
    if not app.get_access_token():
        app.log_automation(
            f"{label}: Zerodha token missing — signal {signal} ignored. Reconnect required.",
            level="WARNING"
        )
        try:
            app.send_telegram(
                f"🔑 <b>{label} SKIPPED — Zerodha token expired</b>\n"
                f"Signal: {signal} | Spot: {spot}\n"
                f"Please reconnect Zerodha."
            )
        except Exception:
            pass
        return

    # ── 2. LOAD STATE ─────────────────────────────────────────────────
    try:
        data   = app.kv_get(config["storage"], {}) or {}
        trades = data.get("trades", [])
        # Use config from DB if present, otherwise use defaults
        db_cfg = data.get("config", {})
        capital  = float(db_cfg.get("capital") or config["capital"])
        risk_pct = float(db_cfg.get("risk_per_trade") or db_cfg.get("risk_factor") or config["risk_pct"])

        # Find ALL open trades (handles orphans/duplicates)
        open_list = find_all_open_trades(trades)

        # ── 3. DECISION ───────────────────────────────────────────────
        if open_list:
            # Check if any open trade matches new signal direction
            latest_idx, latest_trade = open_list[-1]
            if latest_trade.get("trend") == signal and len(open_list) == 1:
                app.log_automation(f"{label}: same signal {signal} — no action", level="INFO")
                return

            # Signal flip OR orphan duplicates exist → close ALL open trades
            for idx, trade in open_list:
                _close_trade_inline(app, module_name, trades, idx, trade, spot, today,
                                    reason="signal_flip" if latest_trade.get("trend") != signal else "orphan_cleanup")
                app.log_automation(
                    f"{label}: closed trade [{idx}] (was {trade.get('trend')}, new signal {signal})",
                    level="INFO"
                )

        # ── ENTRY CUTOFF — no NEW positions within the final minutes before
        # square-off (NSE CAS extended close). Closes above still happened. ──
        if _past_entry_cutoff():
            data["trades"] = trades
            app.kv_set(config["storage"], data)
            app.log_automation(
                f"{label}: signal {signal} received after entry cutoff (3:15 PM) — no new position opened",
                level="WARNING"
            )
            try:
                app.send_telegram(
                    f"⏱️ <b>{label} — entry skipped</b>\n"
                    f"Signal: {signal} | Spot: {spot}\n"
                    f"Too close to square-off (after 3:15 PM) — no new position opened."
                )
            except Exception:
                pass
            return

        # Open new position
        new_idx, new_trade, qty = _open_trade_inline(
            app, module_name, trades, signal, spot, signal_time, capital, risk_pct,
            source="signal"
        )
        if new_trade is None:
            # Failed to compute qty / fetch quote — skip but persist closes
            data["trades"] = trades
            app.kv_set(config["storage"], data)
            return

        app.log_automation(
            f"{label}: opened {signal} trade [{new_idx}] qty={qty}",
            level="INFO"
        )

        # ── 4. ATOMIC WRITE ───────────────────────────────────────────
        data["trades"] = trades
        # Update config marker for UI
        cfg = data.get("config", {})
        cfg["capital"] = capital
        if config["type"] == "credit_spread":
            cfg["risk_factor"] = risk_pct
        else:
            cfg["risk_per_trade"] = risk_pct
        cfg["signal_source"] = "TRADINGVIEW"
        cfg["signal_time"]   = signal_time
        data["config"] = cfg

        app.kv_set(config["storage"], data)

        # ── 5. UPDATE POSITIONS CACHE (UI only) ───────────────────────
        update_positions_cache(app, module_name, config["channel"], new_trade, new_idx, qty)

        # ── 6. TELEGRAM ───────────────────────────────────────────────
        _send_entry_telegram(app, module_name, signal, new_trade, qty)

    except Exception as e:
        app.log_automation(
            f"{label}: _process_model_signal ERROR: {e}\n{traceback.format_exc()}",
            level="ERROR"
        )


def _quote_directional_for_close(app, trade, spot, expiry_for_close):
    """Price the EXACT held contract on close. Falls back to re-derive only for
    legacy trades that never stored a tradingsymbol."""
    symbol = trade.get("tradingsymbol")
    if symbol:
        try:
            q = app.quote_option_by_symbol(symbol)
            return float(q.get("premium") or 0.05)
        except Exception as e:
            app.log_automation(f"quote_option_by_symbol failed for {symbol}: {e} — falling back", level="WARNING")
    # Legacy fallback: re-derive from spot (only for old trades with no symbol)
    exit_q = safe_quote_directional(app, trade.get("trend"), spot, expiry_for_close)
    return float(exit_q.get("premium") or 0.05)


def _quote_spread_for_close(app, trade, spot, expiry_for_close):
    """Price the EXACT held spread legs on close. Falls back to re-derive only
    for legacy trades that never stored leg symbols."""
    atm_sym = trade.get("atm_tradingsymbol")
    otm_sym = trade.get("otm_tradingsymbol")
    if atm_sym and otm_sym:
        try:
            q = app.get_spread_quote_by_symbols(atm_sym, otm_sym)
            return (float(q.get("atm_sell_premium") or 0.05),
                    float(q.get("otm_buy_premium") or 0.05))
        except Exception as e:
            app.log_automation(f"get_spread_quote_by_symbols failed: {e} — falling back", level="WARNING")
    close_q = safe_quote_spread(app, trade.get("trend"), spot, expiry_for_close)
    return (float(close_q.get("atm_sell_premium") or 0.05),
            float(close_q.get("otm_buy_premium") or 0.05))


def _close_trade_inline(app, module_name, trades, idx, trade, spot, today, reason="signal_flip"):
    """Mutate trades[idx] in place — mark CLOSED with exit prices.

    Prices the ACTUAL held contract (by stored tradingsymbol), not a contract
    re-derived from close-time spot. This prevents the bug where a trade opened
    at one strike gets closed at a different strike's price after spot moved.
    """
    config = MODEL_CONFIG[module_name]
    expiry_for_close = trade.get("expiry") or app.get_nearest_nifty_weekly_expiry()

    if config["type"] == "directional_buy":
        exit_price = _quote_directional_for_close(app, trade, spot, expiry_for_close)
        trades[idx].update({
            "exit_price":  exit_price,
            "exit_date":   today,
            "status":      "CLOSED",
            "exit_reason": reason,
        })
    else:  # credit_spread
        atm_exit, otm_exit = _quote_spread_for_close(app, trade, spot, expiry_for_close)
        trades[idx].update({
            "atm_exit_price": atm_exit,
            "otm_exit_price": otm_exit,
            "exit_date":      today,
            "status":         "CLOSED",
            "exit_reason":    reason,
        })

    # LIVE hook — no-op unless this workstation model is live-enabled
    if _is_live_for(app, module_name):
        _live_exit(app, module_name, trades[idx], trades[idx].get("qty"))


def _entry_expiry_with_rollover_rule(app, expiry, now_ist=None):
    """Primer rollover rule for new entries.

    If the given (nearest) expiry is TODAY and the current IST time is at/after
    3:15 PM, the primer says the new position belongs to the NEXT weekly expiry
    series. Otherwise the expiry is returned unchanged.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _IST = _tz(_td(hours=5, minutes=30))
    if now_ist is None:
        now_ist = _dt.now(_IST)
    today_iso = now_ist.date().isoformat()
    if expiry == today_iso:
        hhmm = now_ist.hour * 100 + now_ist.minute
        if hhmm >= 1515:
            return app.get_next_nifty_weekly_expiry_after(expiry)
    return expiry


def _open_trade_inline(app, module_name, trades, signal, spot, signal_time, capital, risk_pct,
                       source="signal", expiry=None):
    """Append a new OPEN trade to trades list. Returns (idx, trade_dict, qty) or (None, None, 0)."""
    config = MODEL_CONFIG[module_name]
    if not expiry:
        expiry = _entry_expiry_with_rollover_rule(app, app.get_nearest_nifty_weekly_expiry())

    # Drawdown-scaled risk: size the new entry off the model's running drawdown
    # so risk shrinks after losses (OB 7/4.2/1.4%, AIT 10/6%, NiftyEXP 12/7.2%).
    dd = model_current_drawdown(module_name, trades, capital)

    if config["type"] == "directional_buy":
        quote = safe_quote_directional(app, signal, spot, expiry)
        premium = float(quote.get("premium") or 0.0)
        qty = compute_qty_directional(capital, risk_pct, premium, dd=dd)
        if qty <= 0 or premium <= 0:
            app.log_automation(
                f"{config['label']}: cannot open — qty={qty} premium={premium} (dd={dd:.1%})",
                level="WARNING"
            )
            return None, None, 0
        new_trade = build_directional_trade(signal, spot, expiry, quote, qty,
                                             capital, risk_pct, signal_time, source)
    else:
        spread = safe_quote_spread(app, signal, spot, expiry)
        qty = compute_qty_spread(capital, risk_pct, dd=dd)
        new_trade = build_spread_trade(signal, spot, expiry, spread, qty,
                                        capital, risk_pct, signal_time, source)

    trades.append(new_trade)
    new_idx = len(trades) - 1
    # LIVE hook — no-op unless this workstation model is live-enabled
    if _is_live_for(app, module_name):
        _live_entry(app, module_name, new_trade, qty)
    return new_idx, new_trade, qty


def _send_entry_telegram(app, module_name, signal, trade, qty):
    config = MODEL_CONFIG[module_name]
    try:
        if config["type"] == "directional_buy":
            app.send_telegram(
                f"📈 <b>{config['label']} ENTRY</b>\n"
                f"Signal: {signal} | {trade.get('type')}: {trade.get('tradingsymbol')}\n"
                f"Premium: {trade.get('entry_price')} | Qty: {qty} | Expiry: {trade.get('expiry')}"
            )
        else:
            app.send_telegram(
                f"📊 <b>{config['label']} ENTRY</b>\n"
                f"Signal: {signal} | {trade.get('type')} Spread\n"
                f"Sell ATM: {trade.get('atm_tradingsymbol')} @ {trade.get('atm_sell_price')}\n"
                f"Buy OTM:  {trade.get('otm_tradingsymbol')} @ {trade.get('otm_buy_price')}\n"
                f"Qty: {qty} | Expiry: {trade.get('expiry')}"
            )
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# NIFTY EXP — special time-windowed logic
# ════════════════════════════════════════════════════════════════════════════

def _is_monday_entry_window(now_ist):
    """Monday 15:14–15:29 IST. Widened from a 7-min to a 15-min catch-up
    window — a single transient failure (token/spot fetch) partway through
    used to mean the entry was silently skipped for the whole week; the
    per-day idempotency marker still guarantees it only fires once."""
    if now_ist.weekday() != 0:
        return False
    hhmm = now_ist.hour * 100 + now_ist.minute
    return 1514 <= hhmm <= 1529


def _is_tuesday_exit_window(now_ist):
    """Tuesday 15:14–15:21 IST."""
    if now_ist.weekday() != 1:
        return False
    hhmm = now_ist.hour * 100 + now_ist.minute
    return 1514 <= hhmm <= 1521












# ════════════════════════════════════════════════════════════════════════════
# ROLLOVER — OB / AIT / OBW / AITW on Tuesday 3:15 PM IST
# ════════════════════════════════════════════════════════════════════════════

def scheduler_expiry_rollover(app, module_name):
    """Tuesday 3:15 PM IST — close current expiry trade, open same-direction trade on next week's expiry.

    Used by: options_buy, options_ait, ob_workstation, ait_workstation.
    NOT used by: nifty_exp (which has its own no-rollover logic).
    """
    config = MODEL_CONFIG[module_name]
    label  = config["label"]
    today  = _today_iso()

    data = app.kv_get(config["storage"], {}) or {}
    cfg  = data.get("config", {})
    if cfg.get("last_rollover_date") == today:
        app.log_automation(f"{label}: rollover already done today — skipping", level="INFO")
        return

    if not app.get_access_token():
        app.log_automation(f"{label}: rollover skipped — Zerodha token missing", level="WARNING")
        return

    spot = fetch_live_nifty_spot(app)
    if spot <= 0:
        app.log_automation(f"{label}: rollover skipped — could not fetch live spot", level="WARNING")
        return

    try:
        trades = data.get("trades", [])
        open_list = find_all_open_trades(trades)

        if not open_list:
            app.log_automation(f"{label}: rollover — no open trades to roll", level="INFO")
            cfg["last_rollover_date"] = today
            data["config"] = cfg
            app.kv_set(config["storage"], data)
            return

        # Use direction from latest open trade (NOT master_state — could be stale or different channel)
        latest_idx, latest_trade = open_list[-1]
        signal = str(latest_trade.get("trend", "")).upper()
        if signal not in ("LONG", "SHORT"):
            app.log_automation(f"{label}: rollover skipped — invalid signal in open trade: {signal}", level="WARNING")
            return

        # Close ALL open trades (handles orphans)
        for idx, trade in open_list:
            _close_trade_inline(app, module_name, trades, idx, trade, spot, today,
                                reason="expiry_rollover")
            app.log_automation(f"{label}: rollover — closed trade [{idx}]", level="INFO")

        # Open new trade on NEXT week's expiry
        cur_expiry  = app.get_nearest_nifty_weekly_expiry()
        next_expiry = app.get_next_nifty_weekly_expiry_after(cur_expiry)

        capital  = float(cfg.get("capital") or config["capital"])
        risk_pct = float(cfg.get("risk_per_trade") or cfg.get("risk_factor") or config["risk_pct"])

        new_idx, new_trade, qty = _open_trade_inline(
            app, module_name, trades, signal, spot, app.now_utc_iso(), capital, risk_pct,
            source="expiry_rollover", expiry=next_expiry
        )

        data["trades"] = trades
        cfg["last_rollover_date"] = today
        cfg["capital"] = capital
        if config["type"] == "credit_spread":
            cfg["risk_factor"] = risk_pct
        else:
            cfg["risk_per_trade"] = risk_pct
        data["config"] = cfg
        app.kv_set(config["storage"], data)

        if new_trade is not None:
            update_positions_cache(app, module_name, config["channel"], new_trade, new_idx, qty)
            app.log_automation(f"{label}: rollover — opened {signal} trade [{new_idx}] on next expiry", level="INFO")
            try:
                app.send_telegram(
                    f"🔄 <b>{label} EXPIRY ROLL</b>\n"
                    f"Closed current expiry, opened {signal} on {next_expiry}\n"
                    f"Qty: {qty}"
                )
            except Exception:
                pass
        else:
            update_positions_cache(app, module_name, config["channel"], None)

    except Exception as e:
        app.log_automation(f"{label} rollover ERROR: {e}\n{traceback.format_exc()}", level="ERROR")


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS — called from app.py
# ════════════════════════════════════════════════════════════════════════════

def handle_main_signal(app, signal, spot, signal_time):
    """RETIRED 2026-07-24 — legacy models Options Buy / Options AIT / Nifty EXP
    were removed at the user's request. This main (ATR 17/0.9) webhook no longer
    drives any model. Kept as a no-op so the app.py webhook caller stays valid.
    Workstation models are driven by handle_workstation_signal + scheduler_tick."""
    app.log_automation(
        f"handle_main_signal: legacy models retired — signal {signal} ignored (no-op)",
        level="INFO")
    return


def handle_workstation_signal(app, signal, spot, signal_time):
    """Workstation TradingView webhook (ATR 2 / 2.7) — routes to OBW, AITW.

    NiftyEXP Workstation is intentionally NOT wired here — reverted 2026-08-07.
    Its design: it FOLLOWS the prevailing OB/AIT direction (see
    _prevailing_workstation_direction, used by scheduler_nexp_workstation_monday_entry),
    but only ACTS on its own two fixed weekly checkpoints — enter Monday 15:14,
    hard-exit Tuesday, no rollover. It does not react to every intraday signal
    tick the way OB/AIT do. (A prior change this session briefly wired it into
    this real-time path, which was incorrect — reverted per user correction.)"""
    _process_model_signal(app, "ob_workstation",  signal, spot, signal_time)
    _process_model_signal(app, "ait_workstation", signal, spot, signal_time)


def _prevailing_workstation_direction(app):
    """Return the direction (LONG/SHORT) currently held by OB/AIT Workstation.

    This is the prevailing ATR 2/2.7 workstation signal — read from the trend of
    the current open OBW (then AITW) trade. Falls back to the last closed OBW
    trade's trend, then None.
    """
    for module in ("ob_workstation", "ait_workstation"):
        data = app.kv_get(MODEL_CONFIG[module]["storage"], {}) or {}
        trades = data.get("trades", [])
        open_list = find_all_open_trades(trades)
        if open_list:
            t = str(open_list[-1][1].get("trend", "")).upper()
            if t in ("LONG", "SHORT"):
                return t
    data = app.kv_get(MODEL_CONFIG["ob_workstation"]["storage"], {}) or {}
    trades = data.get("trades", [])
    if trades:
        t = str(trades[-1].get("trend", "")).upper()
        if t in ("LONG", "SHORT"):
            return t
    return None


def scheduler_nexp_workstation_monday_entry(app):
    """Monday 3:15 PM IST — open a NiftyEXP-Workstation spread using the prevailing
    OB/AIT Workstation (ATR 2/2.7) direction + live spot."""
    config = MODEL_CONFIG["nexp_workstation"]
    today  = _today_iso()
    label  = config["label"]

    data = app.kv_get(config["storage"], {}) or {}
    cfg  = data.get("config", {})
    if cfg.get("last_monday_entry_date") == today:
        app.log_automation(f"{label}: Monday entry already done today — skipping", level="INFO")
        return

    if not app.get_access_token():
        app.log_automation(f"{label}: Monday entry skipped — Zerodha token missing", level="WARNING")
        return

    spot = fetch_live_nifty_spot(app)
    if spot <= 0:
        app.log_automation(f"{label}: Monday entry skipped — could not fetch live spot", level="WARNING")
        return

    signal = _prevailing_workstation_direction(app)
    if signal not in ("LONG", "SHORT"):
        app.log_automation(f"{label}: Monday entry skipped — no prevailing workstation direction", level="WARNING")
        return

    try:
        trades = data.get("trades", [])

        open_list = find_all_open_trades(trades)
        for idx, trade in open_list:
            _close_trade_inline(app, "nexp_workstation", trades, idx, trade, spot, today,
                                reason="orphan_pre_monday_entry")
            app.log_automation(f"{label}: closed orphan trade [{idx}] before Monday entry", level="WARNING")

        capital  = float(cfg.get("capital") or config["capital"])
        risk_pct = float(cfg.get("risk_factor") or config["risk_pct"])
        expiry = app.get_nearest_nifty_weekly_expiry()
        signal_time = app.now_utc_iso()

        new_idx, new_trade, qty = _open_trade_inline(
            app, "nexp_workstation", trades, signal, spot, signal_time, capital, risk_pct,
            source="monday_entry", expiry=expiry
        )

        data["trades"] = trades
        cfg["capital"]                = capital
        cfg["risk_factor"]            = risk_pct
        cfg["signal_source"]          = "WORKSTATION_PREVAILING"
        cfg["signal_time"]            = signal_time
        cfg["last_monday_entry_date"] = today
        data["config"] = cfg
        app.kv_set(config["storage"], data)

        if new_trade is not None:
            update_positions_cache(app, "nexp_workstation", "workstation", new_trade, new_idx, qty)
            app.log_automation(f"{label}: Monday entry — opened {signal} spread @ spot {spot}", level="INFO")
            _send_entry_telegram(app, "nexp_workstation", signal, new_trade, qty)
        else:
            update_positions_cache(app, "nexp_workstation", "workstation", None)

    except Exception as e:
        app.log_automation(f"{label} Monday entry ERROR: {e}\n{traceback.format_exc()}", level="ERROR")


def scheduler_nexp_workstation_tuesday_exit(app):
    """Tuesday 3:15 PM IST — close all open NiftyEXP-Workstation trades. NO ROLLOVER."""
    config = MODEL_CONFIG["nexp_workstation"]
    today  = _today_iso()
    label  = config["label"]

    data = app.kv_get(config["storage"], {}) or {}
    cfg  = data.get("config", {})
    if cfg.get("last_tuesday_exit_date") == today:
        app.log_automation(f"{label}: Tuesday exit already done today — skipping", level="INFO")
        return

    if not app.get_access_token():
        app.log_automation(f"{label}: Tuesday exit skipped — Zerodha token missing", level="WARNING")
        return

    spot = fetch_live_nifty_spot(app)
    if spot <= 0:
        app.log_automation(f"{label}: Tuesday exit skipped — could not fetch live spot", level="WARNING")
        return

    try:
        trades = data.get("trades", [])
        open_list = find_all_open_trades(trades)
        if not open_list:
            app.log_automation(f"{label}: Tuesday exit — no open trades", level="INFO")
        else:
            for idx, trade in open_list:
                _close_trade_inline(app, "nexp_workstation", trades, idx, trade, spot, today,
                                    reason="tuesday_final_exit")
                app.log_automation(f"{label}: closed trade [{idx}] (Tuesday final exit)", level="INFO")
            try:
                app.send_telegram(
                    f"\u2705 <b>{label} FINAL EXIT (Tue 3:15 PM)</b>\n"
                    f"Closed {len(open_list)} open trade(s)\n"
                    f"No rollover — next entry next Monday 3:15 PM"
                )
            except Exception:
                pass

        data["trades"] = trades
        cfg["last_tuesday_exit_date"] = today
        data["config"] = cfg
        app.kv_set(config["storage"], data)
        update_positions_cache(app, "nexp_workstation", "workstation", None)

    except Exception as e:
        app.log_automation(f"{label} Tuesday exit ERROR: {e}\n{traceback.format_exc()}", level="ERROR")


# ════════════════════════════════════════════════════════════════════════════
# NIFTY STRANGLE 3.5% — weekly short strangle, Wed 09:20 AM entry -> the
# FOLLOWING Tuesday ~15:38 exit (held through the week, 6 days, to that
# contract's own weekly expiry — corrected 2026-08-07; a same-day-close
# version of this shipped briefly and was wrong). Naked SELL CE (spot +3.5%)
# + SELL PE (spot -3.5%), each leg independently managed with its own Target /
# Stop Loss / Trailing SL / Re-entry throughout the hold. No rollover after
# the Tuesday exit — the next cycle only starts the following Wednesday.
# Unlike the AIT/NiftyEXP "credit_spread" type (sell ATM + buy OTM hedge,
# defined risk), both legs here are naked sells — margin and worst-case risk
# are materially higher; size accordingly via margin_per_lot below.
# ════════════════════════════════════════════════════════════════════════════

NIFTY_STRIKE_INTERVAL = 50  # NIFTY option strikes are quoted in 50-pt steps


def _pct_otm_strike(spot, pct, side):
    """Round spot*(1±pct%) to the nearest tradable 50-pt NIFTY strike.
    side '+' -> above spot (CE leg), '-' -> below spot (PE leg)."""
    raw = spot * (1 + pct / 100.0) if side == "+" else spot * (1 - pct / 100.0)
    return int(round(raw / NIFTY_STRIKE_INTERVAL) * NIFTY_STRIKE_INTERVAL)


def compute_qty_strangle(lots, lot_size=NIFTY_LOT_SIZE):
    """Naked-strangle sizing: both legs get the SAME qty = a FIXED number of
    lots (user-set, default 10) x lot_size. Both legs are already ~3.5% OTM
    sells, so margin is sized off the lot count directly rather than a
    capital/risk_pct/margin_per_lot formula — per user decision 2026-08-07,
    replacing the earlier risk-budget formula (which also had a double-
    division bug that always rounded to 0 lots at the old defaults)."""
    try:
        lots = int(lots or 0)
    except (TypeError, ValueError):
        return 0
    return max(lots, 0) * int(lot_size)


def _default_strangle_leg(side):
    """side: '+' (CE, above spot) or '-' (PE, below spot). Mirrors every field
    from the reference leg-editor UI, even the ones this strategy leaves off."""
    return {
        "position":      "SELL",
        "option_type":   "CE" if side == "+" else "PE",
        "expiry":        "weekly",
        "strike_mode":   "pct_of_atm",
        "strike_side":   side,
        "strike_pct":    3.5,
        "target_enabled": False, "target_unit": "points", "target_value": 0,
        "sl_enabled":     False, "sl_unit": "points",     "sl_value": 0,
        "trail_enabled":  False, "trail_unit": "points",  "trail_trigger": 0, "trail_step": 0,
        "reentry_tgt_enabled": False, "reentry_tgt_mode": "RE ASAP", "reentry_tgt_count": 1,
        "reentry_sl_enabled":  False, "reentry_sl_mode":  "RE ASAP", "reentry_sl_count": 1,
        "momentum_enabled": False, "momentum_unit": "points", "momentum_value": 0,
        "range_breakout_enabled": False, "range_end_dte": 0, "range_time": "09:45",
        "range_side": "High", "range_basis": "Strike Price",
    }


DEFAULT_NIFTY_STRANGLE_W_CONFIG = {
    "capital":        1_000_000,
    "lots":           10,       # fixed qty per leg = lots x lot_size (per user decision 2026-08-07)
    "margin_per_lot": 190_000,  # informational only — total margin display, not used for sizing
    "lot_size":       NIFTY_LOT_SIZE,
    "entry_day":      "Wednesday",
    "entry_time":     "09:20",
    "exit_time":      "15:38",  # on the FOLLOWING Tuesday, not entry day — see scheduler_tick
    "legs": {"ce": _default_strangle_leg("+"), "pe": _default_strangle_leg("-")},
}


def _is_wed_entry_window(now_ist):
    """Wednesday 09:20–09:35 IST. Widened from a 2-min to a 15-min catch-up
    window — a single transient failure (token/spot fetch) right at 09:20
    used to mean the strangle never entered for the whole week; the
    per-day idempotency marker (last_entry_date) still guarantees it only
    fires once."""
    return now_ist.weekday() == 2 and now_ist.hour == 9 and 20 <= now_ist.minute <= 35


def _is_market_hours(now_ist):
    hhmm = now_ist.hour * 100 + now_ist.minute
    return now_ist.weekday() < 5 and 915 <= hhmm <= 1537


def scheduler_nifty_strangle_w_entry(app):
    """Wednesday 09:20 IST — open a fresh short strangle: SELL CE (spot+3.5%),
    SELL PE (spot-3.5%), both nearest weekly expiry. Qty is a fixed lot count
    (user-set, default 10) x lot_size, same qty both legs — both legs are
    already ~3.5% OTM sells, so margin is expected to be covered without a
    separate risk-budget formula."""
    config = MODEL_CONFIG["nifty_strangle_w"]
    label  = config["label"]
    today  = _today_iso()

    data = app.kv_get(config["storage"], {}) or {}
    cfg  = data.get("config") or {}
    if not cfg:
        cfg = {k: v for k, v in DEFAULT_NIFTY_STRANGLE_W_CONFIG.items() if k != "legs"}
        cfg["legs"] = {k: dict(v) for k, v in DEFAULT_NIFTY_STRANGLE_W_CONFIG["legs"].items()}
    if cfg.get("last_entry_date") == today:
        app.log_automation(f"{label}: entry already done today — skipping", level="INFO")
        return

    if not app.get_access_token():
        app.log_automation(f"{label}: entry skipped — Zerodha token missing", level="WARNING")
        return

    spot = fetch_live_nifty_spot(app)
    if spot <= 0:
        app.log_automation(f"{label}: entry skipped — could not fetch live spot", level="WARNING")
        return

    try:
        trades = data.get("trades", [])

        # Safety: any trade still OPEN here means last week's Tuesday exit
        # never ran (token/spot failure, or every retry in that window
        # failed) — the position is genuinely still live at the broker, not
        # just a stale record. Actually BUY back each open leg (not just mark
        # the record CLOSED) before starting a new cycle, so we don't silently
        # abandon a real naked position while opening a second one on top of it.
        for t in trades:
            if isinstance(t, dict) and t.get("status") == "OPEN":
                legs = t.get("legs") or {}
                for key, leg in legs.items():
                    if isinstance(leg, dict) and leg.get("status") == "OPEN":
                        _close_strangle_leg(app, config, key, leg, "orphan_pre_entry")
                t["legs"] = legs
                t["status"] = "CLOSED"
                t["exit_date"] = today
                app.log_automation(f"{label}: closed orphan trade (last cycle's exit must have failed) before new entry", level="WARNING")

        legs_cfg = cfg.get("legs") or DEFAULT_NIFTY_STRANGLE_W_CONFIG["legs"]
        capital  = float(cfg.get("capital") or DEFAULT_NIFTY_STRANGLE_W_CONFIG["capital"])
        lots     = int(cfg.get("lots") or DEFAULT_NIFTY_STRANGLE_W_CONFIG["lots"])
        lot_size = int(cfg.get("lot_size") or NIFTY_LOT_SIZE)
        qty = compute_qty_strangle(lots, lot_size)
        if qty <= 0:
            app.log_automation(f"{label}: entry skipped — computed qty is 0 "
                                f"(check lots/lot_size config)", level="WARNING")
            return

        expiry = app.get_nearest_nifty_weekly_expiry()
        new_legs = {}
        for key, leg_cfg in legs_cfg.items():
            opt_type = leg_cfg.get("option_type", "CE" if key == "ce" else "PE")
            side     = leg_cfg.get("strike_side", "+" if key == "ce" else "-")
            pct      = float(leg_cfg.get("strike_pct") or 3.5)
            strike   = _pct_otm_strike(spot, pct, side)
            try:
                contract = app.pick_nifty_option_contract_at_strike(opt_type, strike, expiry)
                q = app.quote_option_by_symbol(contract["tradingsymbol"])
                entry_price = float(q.get("premium") or 0)
            except Exception as e:
                app.log_automation(f"{label}: {key} contract/quote lookup failed: {e}", level="ERROR")
                continue

            r = app.place_live_order_with_retry("SELL", contract["tradingsymbol"], qty,
                                                  reason=f"nifty_strangle_w_entry_{key}")
            if not r.get("ok"):
                app.log_automation(f"{label}: {key} SELL order failed — {r.get('error')}", level="ERROR")
                continue

            new_legs[key] = {
                "tradingsymbol": contract["tradingsymbol"],
                "strike":        strike,
                "option_type":   opt_type,
                "entry_price":   entry_price,
                "exit_price":    None,
                "qty":           qty,
                "status":        "OPEN",
                "reentry_count": 0,
                "trail_armed":   False,
                "trail_stop":    None,
                "config":        leg_cfg,
            }

        if not new_legs:
            app.log_automation(f"{label}: entry aborted — no legs opened", level="ERROR")
            return

        new_trade = {"date": today, "spot": spot, "expiry": expiry, "status": "OPEN", "legs": new_legs}
        trades.append(new_trade)
        data["trades"] = trades
        cfg["last_entry_date"] = today
        data["config"] = cfg
        app.kv_set(config["storage"], data)
        update_positions_cache(app, "nifty_strangle_w", "workstation",
                                {"trend": "SHORT_STRANGLE"}, len(trades) - 1, qty)

        try:
            leg_lines = "\n".join(f"SELL {l['option_type']} {l['strike']} @ {l['entry_price']}"
                                   for l in new_legs.values())
            app.send_telegram(f"📊 <b>{label} ENTRY</b>\n{leg_lines}\nQty: {qty} | Expiry: {expiry}")
        except Exception:
            pass
        app.log_automation(f"{label}: entry — opened {len(new_legs)} leg(s) @ spot {spot}", level="INFO")

    except Exception as e:
        app.log_automation(f"{label} entry ERROR: {e}\n{traceback.format_exc()}", level="ERROR")


def _close_strangle_leg(app, config, key, leg, exit_reason, exit_price=None):
    """BUY back a short leg to close it (it was sold)."""
    label = config["label"]
    try:
        if exit_price is None:
            q = app.quote_option_by_symbol(leg["tradingsymbol"])
            exit_price = float(q.get("premium") or 0)
        r = app.place_live_order_with_retry("BUY", leg["tradingsymbol"], leg["qty"],
                                              reason=f"nifty_strangle_w_exit_{key}_{exit_reason}")
        leg["exit_price"]  = exit_price
        leg["status"]      = "CLOSED"
        leg["exit_reason"] = exit_reason
        app.log_automation(f"{label}: {key} leg closed ({exit_reason}) @ {exit_price}", level="INFO")
        try:
            app.send_telegram(f"⚡ <b>{label} {key.upper()} EXIT</b>\nReason: {exit_reason}\n"
                               f"{leg['option_type']} {leg['strike']} @ {exit_price}")
        except Exception:
            pass
        return r.get("ok", False)
    except Exception as e:
        app.log_automation(f"{label}: {key} leg close ERROR: {e}", level="ERROR")
        return False


def _reenter_strangle_leg(app, config, key, closed_leg, spot, expiry, trigger):
    """Open a fresh replacement leg after a Target/SL exit, if re-entry is
    enabled for that trigger and the per-cycle re-entry count isn't used up."""
    label = config["label"]
    leg_cfg = closed_leg.get("config") or {}
    if trigger == "target":
        enabled, max_count = leg_cfg.get("reentry_tgt_enabled"), int(leg_cfg.get("reentry_tgt_count") or 1)
    else:
        enabled, max_count = leg_cfg.get("reentry_sl_enabled"), int(leg_cfg.get("reentry_sl_count") or 1)
    if not enabled or int(closed_leg.get("reentry_count") or 0) >= max_count:
        return None

    opt_type = leg_cfg.get("option_type", closed_leg.get("option_type"))
    side     = leg_cfg.get("strike_side", "+" if opt_type == "CE" else "-")
    pct      = float(leg_cfg.get("strike_pct") or 3.5)
    strike   = _pct_otm_strike(spot, pct, side)
    try:
        contract = app.pick_nifty_option_contract_at_strike(opt_type, strike, expiry)
        q = app.quote_option_by_symbol(contract["tradingsymbol"])
        entry_price = float(q.get("premium") or 0)
        r = app.place_live_order_with_retry("SELL", contract["tradingsymbol"], closed_leg["qty"],
                                              reason=f"nifty_strangle_w_reentry_{key}")
        if not r.get("ok"):
            app.log_automation(f"{label}: {key} re-entry order failed — {r.get('error')}", level="ERROR")
            return None
    except Exception as e:
        app.log_automation(f"{label}: {key} re-entry lookup failed: {e}", level="ERROR")
        return None

    new_leg = {
        "tradingsymbol": contract["tradingsymbol"],
        "strike":        strike,
        "option_type":   opt_type,
        "entry_price":   entry_price,
        "exit_price":    None,
        "qty":           closed_leg["qty"],
        "status":        "OPEN",
        "reentry_count": int(closed_leg.get("reentry_count") or 0) + 1,
        "trail_armed":   False,
        "trail_stop":    None,
        "config":        leg_cfg,
    }
    app.log_automation(f"{label}: {key} re-entered ({trigger}) — SELL {opt_type} {strike} @ {entry_price}",
                        level="INFO")
    try:
        app.send_telegram(f"🔁 <b>{label} {key.upper()} RE-ENTRY</b>\nSELL {opt_type} {strike} @ {entry_price}")
    except Exception:
        pass
    return new_leg


def monitor_nifty_strangle_w_legs(app):
    """Called every scheduler tick during market hours. For the currently open
    Wednesday cycle, evaluates each OPEN leg's Target/Stop-Loss/Trailing-SL
    against its live premium and closes/re-enters as configured.

    NOTE — scope: Target Profit, Stop Loss, Trailing SL and Re-entry (on Target
    and on SL) are implemented below with standard, unambiguous definitions.
    Simple Momentum and Range Breakout are saved as config (present in the UI,
    per-leg) but are NOT wired to execution logic here — their reference fields
    (a Points value; an End DTE/time/High-Low/Strike-Price basis) don't have an
    agreed operational rule yet. Define exactly what each should do and this
    monitor is the place to add them.

    Independent of the fixed 15:38 square-off, which is a separate hard
    backstop (scheduler_nifty_strangle_w_squareoff)."""
    config = MODEL_CONFIG["nifty_strangle_w"]
    label  = config["label"]
    today  = _today_iso()

    data = app.kv_get(config["storage"], {}) or {}
    trades = data.get("trades", [])
    open_list = find_all_open_trades(trades)
    if not open_list:
        return
    if not app.get_access_token():
        return

    idx, trade = open_list[-1]
    legs = trade.get("legs") or {}
    if not legs or all(l.get("status") != "OPEN" for l in legs.values()):
        return

    spot = fetch_live_nifty_spot(app)
    expiry = trade.get("expiry")
    changed = False

    for key, leg in list(legs.items()):
        if leg.get("status") != "OPEN":
            continue
        leg_cfg = leg.get("config") or {}
        try:
            q = app.quote_option_by_symbol(leg["tradingsymbol"])
            ltp = float(q.get("premium") or 0)
        except Exception as e:
            app.log_automation(f"{label}: {key} monitor quote failed: {e}", level="WARNING")
            continue
        if ltp <= 0:
            continue

        entry = float(leg.get("entry_price") or 0)
        decay = entry - ltp   # short option: profit when premium falls
        exit_reason = None

        # Trailing SL: once decay passes `trail_trigger`, a stop trails `trail_step`
        # points behind the best (lowest) premium seen since — protects profit
        # rather than just capping loss.
        if leg_cfg.get("trail_enabled"):
            trigger = float(leg_cfg.get("trail_trigger") or 0)
            step    = float(leg_cfg.get("trail_step") or 0)
            if not leg.get("trail_armed") and trigger > 0 and decay >= trigger:
                leg["trail_armed"] = True
                leg["trail_stop"]  = ltp + step
                changed = True
            elif leg.get("trail_armed"):
                candidate_stop = ltp + step
                if candidate_stop < (leg.get("trail_stop") if leg.get("trail_stop") is not None else float("inf")):
                    leg["trail_stop"] = candidate_stop
                    changed = True
                if leg.get("trail_stop") is not None and ltp >= leg["trail_stop"]:
                    exit_reason = "trail_sl"

        # Target Profit (points of premium decay from entry)
        if not exit_reason and leg_cfg.get("target_enabled"):
            tgt = float(leg_cfg.get("target_value") or 0)
            if tgt > 0 and decay >= tgt:
                exit_reason = "target"

        # Stop Loss (points premium has risen against the short, from entry)
        if not exit_reason and leg_cfg.get("sl_enabled"):
            sl = float(leg_cfg.get("sl_value") or 0)
            if sl > 0 and (ltp - entry) >= sl:
                exit_reason = "stop_loss"

        if exit_reason:
            ok = _close_strangle_leg(app, config, key, leg, exit_reason, exit_price=ltp)
            changed = True
            trigger_kind = "target" if exit_reason in ("target", "trail_sl") else "sl"
            if ok:
                new_leg = _reenter_strangle_leg(app, config, key, leg, spot, expiry, trigger_kind)
                if new_leg:
                    legs[key] = new_leg

    if changed:
        trade["legs"] = legs
        if all(l.get("status") == "CLOSED" for l in legs.values()):
            trade["status"] = "CLOSED"
            trade["exit_date"] = today
            update_positions_cache(app, "nifty_strangle_w", "workstation", None)
        trades[idx] = trade
        data["trades"] = trades
        app.kv_set(config["storage"], data)


def scheduler_nifty_strangle_w_squareoff(app):
    """Tuesday ~15:31-15:39 IST — force-close whatever's been held since the
    PRIOR Wednesday's entry (hard backstop, independent of each leg's own
    Target/SL/Trail). Reverted 2026-08-07 to the correct spec: this is a
    6-day hold (Wed entry -> following Tue exit, at the contract's own weekly
    expiry), not a same-day close — a prior version of this model incorrectly
    closed same-day at 15:38, which has since been corrected. No rollover —
    next entry is the following Wednesday 09:20 AM."""
    config = MODEL_CONFIG["nifty_strangle_w"]
    label  = config["label"]
    today  = _today_iso()

    data = app.kv_get(config["storage"], {}) or {}
    cfg  = data.get("config") or {}
    if cfg.get("last_squareoff_date") == today:
        return

    trades = data.get("trades", [])
    open_list = find_all_open_trades(trades)
    if not open_list:
        cfg["last_squareoff_date"] = today
        data["config"] = cfg
        app.kv_set(config["storage"], data)
        return
    if not app.get_access_token():
        app.log_automation(f"{label}: 15:38 square-off skipped — Zerodha token missing", level="WARNING")
        return

    try:
        idx, trade = open_list[-1]
        legs = trade.get("legs") or {}
        closed_any = False
        for key, leg in legs.items():
            if leg.get("status") == "OPEN":
                _close_strangle_leg(app, config, key, leg, "eod_squareoff")
                closed_any = True

        trade["legs"] = legs
        trade["status"] = "CLOSED"
        trade["exit_date"] = today
        trades[idx] = trade
        data["trades"] = trades
        cfg["last_squareoff_date"] = today
        data["config"] = cfg
        app.kv_set(config["storage"], data)
        update_positions_cache(app, "nifty_strangle_w", "workstation", None)

        if closed_any:
            try:
                app.send_telegram(f"✅ <b>{label} FINAL SQUARE-OFF (3:38 PM)</b>\nAll open legs closed. "
                                   f"No rollover — next entry next Wednesday 09:20 AM.")
            except Exception:
                pass
        app.log_automation(f"{label}: 15:38 square-off complete", level="INFO")

    except Exception as e:
        app.log_automation(f"{label} square-off ERROR: {e}\n{traceback.format_exc()}", level="ERROR")


def scheduler_tick(app, now_ist):
    """Called every 60s from app.py's position_sync_job.
    Handles Monday entry / Tuesday exit / Tuesday rollover within tight windows,
    plus the Nifty Strangle 3.5% Wednesday entry -> following-Tuesday square-off.
    """
    def _off(m):
        try:
            return app.get_model_mode(m) == "off"
        except Exception:
            return False

    # Monday 15:14–15:21 IST: NiftyEXP Workstation entry (skipped if Off/standby)
    if _is_monday_entry_window(now_ist):
        if not _off("nexp_workstation"):
            scheduler_nexp_workstation_monday_entry(app)

    # Tuesday expiry day — close as near the NSE close as practical, but with
    # real retry room. SEBI/NSE moved the F&O close from 3:30 PM to 3:40 PM
    # effective 3 Aug 2026 (Closing Auction Session rollout) — that hard close
    # is the ceiling both windows must finish well before, so instead of
    # pushing the windows later (which would risk placing orders after close),
    # they now START earlier: 15:30–15:38 for OB/AIT roll, 15:31–15:39 for
    # NiftyEXP final exit. That gives ~8-9 min of retry room for a transient
    # failure (token/spot fetch) instead of the old single-minute checks,
    # while still finishing at least 1 min before the 15:40 close. Each
    # handler's own per-day marker (last_rollover_date / last_tuesday_exit_date)
    # guarantees it still only fires once even though the window is checked
    # every 60s tick.
    if now_ist.weekday() == 1:
        _hhmm = now_ist.hour * 100 + now_ist.minute
        if 1530 <= _hhmm <= 1538:
            if not _off("ob_workstation"):
                scheduler_expiry_rollover(app, "ob_workstation")
            if not _off("ait_workstation"):
                scheduler_expiry_rollover(app, "ait_workstation")
        if 1531 <= _hhmm <= 1539:
            if not _off("nexp_workstation"):
                scheduler_nexp_workstation_tuesday_exit(app)
            # Nifty Strangle 3.5%: holds from LAST Wednesday's 09:20 entry
            # through to this Tuesday's close (its own weekly expiry) — a
            # 6-day hold, not a same-day close (corrected 2026-08-07).
            if not _off("nifty_strangle_w"):
                scheduler_nifty_strangle_w_squareoff(app)

    # Nifty Strangle 3.5% — Wednesday 09:20 entry, then continuous intraday
    # leg monitoring (Target/SL/Trailing-SL/Re-entry) every weekday during
    # market hours all the way through to next Tuesday's square-off above.
    if not _off("nifty_strangle_w"):
        if _is_wed_entry_window(now_ist):
            scheduler_nifty_strangle_w_entry(app)
        if _is_market_hours(now_ist):
            monitor_nifty_strangle_w_legs(app)


# ════════════════════════════════════════════════════════════════════════════
# END OF MODULE
# ════════════════════════════════════════════════════════════════════════════
