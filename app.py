import models_v2
from flask import Flask, render_template, request, jsonify, redirect, session
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import logging
import math
import os
import sqlite3
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from kiteconnect import KiteConnect
except Exception:
    KiteConnect = None

app = Flask(__name__)
from flask import render_template, request, redirect, session, url_for, send_from_directory

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret")

@app.route("/")
def index():
    if "user_id" in session:
        return redirect("/dashboard")
    return redirect("/login")

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('/opt/stairs-web-app/static', filename)

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")
    return render_template("index.html")

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '0') == '1'
app.config['PREFERRED_URL_SCHEME'] = os.environ.get('PREFERRED_URL_SCHEME', 'http')
# Login used to expire the moment the browser closed (Flask's default is a
# non-permanent session cookie). Keep users signed in for 30 days across
# browser restarts — set alongside session['user_id'] at both login sites.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO').upper())

CURRENT_NIFTY_LOT_SIZE = 65
KITE_API_KEY = os.environ.get('KITE_API_KEY')
KITE_API_SECRET = os.environ.get('KITE_API_SECRET')
TRADINGVIEW_WEBHOOK_SECRET = os.environ.get('TRADINGVIEW_WEBHOOK_SECRET', '')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')

DB_PATH = os.environ.get('STAIRS_DB_PATH', os.path.join(os.path.dirname(__file__), 'stairs_state.db'))
AUTOMATION_POLL_SECONDS = int(os.environ.get('AUTOMATION_POLL_SECONDS', '20'))
AUTOMATION_LIVE_ORDERS = os.environ.get('AUTOMATION_LIVE_ORDERS', '0') == '1'
AUTOMATION_PRODUCT = os.environ.get('AUTOMATION_PRODUCT', 'NRML')
AUTOMATION_ORDER_TYPE = os.environ.get('AUTOMATION_ORDER_TYPE', 'MARKET')
# Zerodha mandates market protection on MARKET/SL-M orders via API (SEBI algo rules).
# -1 = automatic protection per Zerodha guidelines. >0..100 = explicit percentage.
AUTOMATION_MARKET_PROTECTION = int(os.environ.get('AUTOMATION_MARKET_PROTECTION', '-1'))
APP_TZ = ZoneInfo(os.environ.get('APP_TIMEZONE', 'Asia/Kolkata'))
automation_lock = threading.RLock()
automation_thread_started = False

# NSE's CAS (Closing Auction Session) change extended the trading/close window
# to ~3:40 PM. No brand-new position should open this close to square-off —
# existing open positions can still be closed/squared-off as normal.
ENTRY_CUTOFF_HHMM = 1515


def _past_entry_cutoff():
    """True if it's at/after 3:15 PM IST — no new entries allowed past this."""
    now_ist = datetime.now(APP_TZ)
    return (now_ist.hour * 100 + now_ist.minute) >= ENTRY_CUTOFF_HHMM


def get_db():
    # wait for a lock rather than raising 'database is locked'
    # immediately (sqlite's default busy_timeout is 0)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def db_init():
    conn = get_db()
    # WAL lives in the db file itself — set once, persists
    conn.execute('PRAGMA journal_mode=WAL')
    cur = conn.cursor()
    cur.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)')
    cur.execute('CREATE TABLE IF NOT EXISTS automation_log (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, details TEXT)')
    conn.commit()
    conn.close()


def now_utc_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'


def kv_get(key, default=None):
    conn = get_db(); cur = conn.cursor(); cur.execute('SELECT value FROM kv WHERE key=?', (key,))
    row = cur.fetchone(); conn.close()
    if not row:
        return default
    try:
        return json.loads(row['value'])
    except Exception:
        return row['value']


def kv_set(key, value):
    payload = json.dumps(value)
    # Log any write that clears trades
    if "strategy_bundle" in key:
        try:
            parsed = json.loads(payload)
            trades = parsed.get("trades", []) if isinstance(parsed, dict) else []
            if len(trades) == 0:
                import traceback
                stack = traceback.format_stack()
                app_frames = [f for f in stack if 'stairs-web-app/app.py' in f]
                log_automation(f"WARNING: kv_set clearing {key} from: {' | '.join(app_frames[-4:])}", level="WARNING")
            else:
                import traceback
                stack = traceback.format_stack()
                app_frames = [f for f in stack if 'stairs-web-app/app.py' in f]
                log_automation(f"INFO: kv_set WRITING {len(trades)} trades to {key} from: {' | '.join(app_frames[-3:])}", level="INFO")
        except Exception:
            pass
    conn = get_db(); cur = conn.cursor()
    cur.execute('INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at', (key, payload, now_utc_iso()))
    conn.commit(); conn.close()


def log_automation(message, level='INFO', details=None):
    app.logger.log(getattr(logging, level.upper(), logging.INFO), message)
    conn = get_db(); cur = conn.cursor()
    cur.execute('INSERT INTO automation_log(created_at, level, message, details) VALUES (?, ?, ?, ?)', (now_utc_iso(), level.upper(), message, json.dumps(details) if details is not None else None))
    conn.commit(); conn.close()


def get_automation_logs(limit=50):
    conn = get_db(); cur = conn.cursor(); cur.execute('SELECT created_at, level, message, details FROM automation_log ORDER BY id DESC LIMIT ?', (limit,))
    rows = [dict(r) for r in cur.fetchall()]; conn.close()
    return rows


def save_master_state():
    kv_set('master_state', master_state)


def save_automation_state():
    kv_set('automation_state', automation_state)


def get_active_position():
    return kv_get('active_position', None)


def set_active_position(value):
    kv_set('active_position', value)


def _kite_token_key(user_id):
    return 'kite_token::%s' % user_id


def get_access_token(user_id=None):
    """Zerodha access token. With user_id -> that user's own token (multi-tenant,
    no cross-account fallback). Without -> the instance/global token the current
    single-account engine uses."""
    if user_id:
        rec = kv_get(_kite_token_key(user_id), None)
        if isinstance(rec, dict):
            return rec.get('access_token')
        return None
    return kv_get('kite_access_token', None)


def get_token_set_at(user_id=None):
    if user_id:
        rec = kv_get(_kite_token_key(user_id), None)
        return rec.get('set_at') if isinstance(rec, dict) else None
    return kv_get('kite_token_set_at', None)


def set_access_token(value, user_id=None):
    # Store token with timestamp so we can detect staleness.
    now = datetime.utcnow().isoformat()
    if user_id:
        kv_set(_kite_token_key(user_id), {'access_token': value, 'set_at': now})
    else:
        kv_set('kite_access_token', value)
        kv_set('kite_token_set_at', now)


def get_user_kite_creds(user_id):
    """That user's own Kite Connect app credentials, if they saved them."""
    rec = kv_get("kite_creds::%s" % user_id, None) if user_id else None
    if isinstance(rec, dict) and rec.get("api_key") and rec.get("api_secret"):
        return rec["api_key"], rec["api_secret"]
    return None


def resolve_kite_creds(user_id=None):
    """Per-user API key/secret if the user has their own Kite Connect app,
    else the global env credentials (admin / single-account instance)."""
    if user_id:
        c = get_user_kite_creds(user_id)
        if c:
            return c
    return os.environ.get("KITE_API_KEY"), os.environ.get("KITE_API_SECRET")


def is_token_fresh():
    """Kite tokens expire at 6 AM IST daily. Check if token was set today after 6 AM IST."""
    token = get_access_token()
    if not token:
        return False
    set_at_str = kv_get('kite_token_set_at', None)
    if not set_at_str:
        return True  # Old token with no timestamp - assume valid
    try:
        set_at = datetime.fromisoformat(set_at_str)
        now_utc = datetime.utcnow()
        # Kite tokens expire at 6 AM IST = 00:30 UTC
        # If token was set before today's 00:30 UTC, it's stale
        today_expiry_utc = now_utc.replace(hour=0, minute=30, second=0, microsecond=0)
        if now_utc < today_expiry_utc:
            # Before 6 AM IST today - use yesterday's expiry
            today_expiry_utc = today_expiry_utc.replace(day=today_expiry_utc.day - 1)
        return set_at > today_expiry_utc
    except Exception:
        return True


def get_automation_config():
    cfg = kv_get('automation_config', None) or {}
    cfg.setdefault('mode', 'LIVE' if AUTOMATION_LIVE_ORDERS else 'DRY_RUN')
    cfg.setdefault('last_run_at', None)
    cfg.setdefault('last_action', None)
    cfg.setdefault('last_error', None)
    return cfg


def set_automation_config(cfg):
    kv_set('automation_config', cfg)


def load_persisted_state():
    persisted_master = kv_get('master_state', None)
    if isinstance(persisted_master, dict):
        master_state.update(persisted_master)
        master_state['nifty_lot_size'] = CURRENT_NIFTY_LOT_SIZE
    persisted_auto = kv_get('automation_state', None)
    if isinstance(persisted_auto, dict):
        automation_state.update({
            'kill_switch': bool(persisted_auto.get('kill_switch')),
            'automation_enabled': bool(persisted_auto.get('automation_enabled')),
        })




def refresh_master_state_from_db():
    persisted_master = kv_get('master_state', None)
    if isinstance(persisted_master, dict):
        master_state.update(persisted_master)
        master_state['nifty_lot_size'] = CURRENT_NIFTY_LOT_SIZE


def persist_master_state_updates(updates: dict):
    persisted_master = kv_get('master_state', None)
    if not isinstance(persisted_master, dict):
        persisted_master = dict(master_state)
    persisted_master.update(updates or {})
    persisted_master['nifty_lot_size'] = CURRENT_NIFTY_LOT_SIZE
    kv_set('master_state', persisted_master)
    master_state.update(persisted_master)
    return persisted_master

def normalize_signal(value):
    signal = str(value or '').strip().upper()
    if signal in ('LONG', 'BUY', 'BULL', 'UP'):
        return 'LONG'
    if signal in ('SHORT', 'SELL', 'BEAR', 'DOWN'):
        return 'SHORT'
    raise RuntimeError(f'Unsupported signal: {value}')




def parse_tradingview_payload():
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    raw = (request.data or b'').decode('utf-8', errors='ignore').strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {'message': raw}


def market_is_open():
    now = datetime.now(APP_TZ)
    if now.weekday() >= 5:
        return False
    hhmm = now.hour * 100 + now.minute
    return 915 <= hhmm <= 1530


def fetch_live_nifty_spot():
    """Fetch live NIFTY 50 spot from Zerodha at the moment of call.
    Used by scheduler-driven handlers to avoid stale master_state reads.
    Falls back to master_state if Zerodha call fails (logs warning).
    """
    try:
        kite = get_kite(require_token=True)
        q = kite.ltp(["NSE:NIFTY 50"])
        ltp = float(q["NSE:NIFTY 50"]["last_price"])
        if ltp > 0:
            return ltp
    except Exception as e:
        log_automation(f"fetch_live_nifty_spot ERROR — falling back to master_state: {e}", level="WARNING")
    return float(master_state.get("nifty_spot") or 0)


def get_nearest_nifty_weekly_expiry():
    rows = get_nfo_instruments()
    today = date.today()
    expiries = sorted({row.get('expiry') for row in rows if row.get('name') == 'NIFTY' and row.get('segment') == 'NFO-OPT' and row.get('expiry') and row.get('expiry') >= today})
    if not expiries:
        raise RuntimeError('No NIFTY weekly/monthly expiries found')
    exp = expiries[0]
    return exp.isoformat() if hasattr(exp, 'isoformat') else str(exp)


def quote_option(signal, spot, expiry=None):
    if not expiry:
        expiry = get_nearest_nifty_weekly_expiry()
    # Directional buy convention: LONG->CE, SHORT->PE.
    # (pick_nifty_option_contract uses the SPREAD convention, which is wrong here.)
    _opt_type = "CE" if str(signal).upper() == "LONG" else "PE"
    contract = pick_nifty_option_contract_by_type(_opt_type, spot, expiry)
    instrument_key = f"{contract['exchange']}:{contract['tradingsymbol']}"
    kite = get_kite(require_token=True)
    q = kite.quote([instrument_key])
    data = q[instrument_key]
    premium = float((data.get('last_price') or 0))
    ts = data.get('timestamp') or data.get('last_trade_time')
    return {
        'contract': contract,
        'instrument_key': instrument_key,
        'premium': premium,
        'quote_timestamp': ts.isoformat() if hasattr(ts, 'isoformat') else (str(ts) if ts else None),
    }


def quote_option_by_symbol(tradingsymbol, exchange="NFO"):
    """Fetch the live premium for an EXACT option contract by tradingsymbol.

    Unlike quote_option (which derives the strike from spot), this prices the
    specific contract that was actually traded — required for correct exits/
    rollovers when spot has moved since entry.
    Returns a dict shaped like quote_option's: {'contract':{...}, 'premium':..}.
    """
    if not tradingsymbol:
        raise RuntimeError("quote_option_by_symbol called with empty tradingsymbol")
    instrument_key = f"{exchange}:{tradingsymbol}"
    kite = get_kite(require_token=True)
    q = kite.quote([instrument_key])
    data = q[instrument_key]
    premium = float((data.get('last_price') or 0))
    ts = data.get('timestamp') or data.get('last_trade_time')
    return {
        'contract': {'tradingsymbol': tradingsymbol, 'exchange': exchange},
        'instrument_key': instrument_key,
        'premium': premium,
        'quote_timestamp': ts.isoformat() if hasattr(ts, 'isoformat') else (str(ts) if ts else None),
    }


def get_spread_quote_by_symbols(atm_tradingsymbol, otm_tradingsymbol, exchange="NFO"):
    """Fetch live premiums for an EXACT credit-spread pair by their tradingsymbols.

    Prices the specific legs that were actually traded, rather than re-deriving
    strikes from current spot. Returns {'atm_sell_premium':.., 'otm_buy_premium':..}.
    """
    if not atm_tradingsymbol or not otm_tradingsymbol:
        raise RuntimeError("get_spread_quote_by_symbols called with empty leg symbol(s)")
    atm_key = f"{exchange}:{atm_tradingsymbol}"
    otm_key = f"{exchange}:{otm_tradingsymbol}"
    kite = get_kite(require_token=True)
    q = kite.quote([atm_key, otm_key])
    atm_data = q[atm_key]
    otm_data = q[otm_key]
    return {
        'atm_tradingsymbol': atm_tradingsymbol,
        'otm_tradingsymbol': otm_tradingsymbol,
        'atm_sell_premium': float(atm_data.get('last_price') or 0),
        'otm_buy_premium': float(otm_data.get('last_price') or 0),
        'quote_time': now_utc_iso(),
    }


def compute_option_quantity(capital, risk_pct, premium, lot_size):
    risk_budget = float(capital) * float(risk_pct)
    cost_per_lot = float(premium) * int(lot_size)
    lots = math.floor(risk_budget / cost_per_lot) if cost_per_lot > 0 else 0
    qty = lots * int(lot_size)
    return {
        'risk_budget': round(risk_budget, 2),
        'cost_per_lot': round(cost_per_lot, 2),
        'lots': int(max(lots, 0)),
        'quantity': int(max(qty, 0)),
    }


def create_order_payload(symbol, transaction_type, quantity):
    return {
        'variety': 'regular',
        'exchange': 'NFO',
        'tradingsymbol': symbol,
        'transaction_type': transaction_type,
        'quantity': int(quantity),
        'product': AUTOMATION_PRODUCT,
        'order_type': AUTOMATION_ORDER_TYPE,
    }


# ============================================================================
# PER-MODEL LIVE-ORDER REGISTRY (hard safety gate)
# Added 2026-07-24 after ORB placed a real order on 2026-07-23 while only
# futures was intended to be live.
#
# ROOT CAUSE: live vs paper was a single GLOBAL switch (automation_config.mode)
# with no per-model gate, so with mode==LIVE EVERY model could place real
# orders. active_models did NOT gate the ORB/options order paths.
#
# HARD RULE: a real order is placed ONLY if BOTH are true:
#   (a) global automation mode == LIVE, and
#   (b) the order's originating model is in `live_enabled_models`.
# Enforced at EVERY order entry point below, so it cannot be bypassed by a
# scheduler bug. Fail-closed: if the model can't be identified from the order
# reason, the order is treated as DRY_RUN (paper) and alerted.
#
# To take a model live: add it to live_enabled_models (persisted in kv), e.g.
# via POST /api/automation/live_models. Default = only futures.
# ============================================================================
KNOWN_LIVE_MODELS = ("futures", "ob_workstation", "ait_workstation", "nexp_workstation", "nifty_strangle_w")
DEFAULT_LIVE_ENABLED_MODELS = ["futures"]

# Which tier-access module a user needs to CONTROL (switch off/paper/live) each
# model. Futures needs 'futures' (Standard+); the workstations need 'automation'
# (Premium+). Admins control everything regardless.
MODEL_ACCESS_MODULE = {
    "futures": "futures",
    "ob_workstation": "automation",
    "ait_workstation": "automation",
    "nexp_workstation": "automation",
    "nifty_strangle_w": "automation",
}

# Per-model execution mode:
#   'off'   = Standby — model does NOT record or trade (no position tracking).
#   'paper' = Sandbox — records paper trades, never places real orders.
#   'live'  = Production — places real Zerodha orders.
MODEL_MODE_VALUES = ("off", "paper", "live")
DEFAULT_MODEL_MODES = {
    "futures":           "live",
    "ob_workstation":    "paper",
    "ait_workstation":   "paper",
    "nexp_workstation":  "paper",
    "nifty_strangle_w":  "paper",   # new strategy — starts in paper per go-live request
}

def _modes_key(user_id=None):
    return ("model_modes::%s" % user_id) if user_id else "model_modes"

def get_model_modes(user_id=None):
    """Per-model execution modes. Pass user_id for that user's modes (multi-tenant);
    omit it for the instance/global modes that the current single-account engine uses."""
    v = kv_get(_modes_key(user_id), None)
    if isinstance(v, dict):
        modes = dict(DEFAULT_MODEL_MODES)
        for m in KNOWN_LIVE_MODELS:
            if v.get(m) in MODEL_MODE_VALUES:
                modes[m] = v[m]
        return modes
    if user_id:
        # A user with no saved modes yet is seeded from the instance modes so the
        # admin sees a starting point, then customises per user.
        return get_model_modes(None)
    # Instance/global first run: preserve the old live_enabled_models selection.
    prev = kv_get("live_enabled_models", None)
    if isinstance(prev, list) and prev:
        return {m: ("live" if m in prev else "paper") for m in KNOWN_LIVE_MODELS}
    return dict(DEFAULT_MODEL_MODES)

def get_model_mode(model, user_id=None):
    return get_model_modes(user_id).get(model, "paper")

def set_model_modes(modes, user_id=None):
    cur = get_model_modes(user_id)
    for m in KNOWN_LIVE_MODELS:
        md = (modes or {}).get(m)
        if md in MODEL_MODE_VALUES:
            cur[m] = md
    kv_set(_modes_key(user_id), cur)
    return cur

def get_live_enabled_models(user_id=None):
    """Derived from per-model modes: only models in 'live' mode place real orders.
    Pass user_id for that user's live models (multi-tenant)."""
    modes = get_model_modes(user_id)
    return [m for m in KNOWN_LIVE_MODELS if modes.get(m) == "live"]

def set_live_enabled_models(models):
    """Back-compat: mapping a live list onto per-model modes (live vs paper).
    Models not named keep their mode unless they were 'live' (then -> paper)."""
    live = set(m for m in (models or []) if m in KNOWN_LIVE_MODELS)
    cur = get_model_modes()
    for m in KNOWN_LIVE_MODELS:
        if m in live:
            cur[m] = "live"
        elif cur.get(m) == "live":
            cur[m] = "paper"
    kv_set("model_modes", cur)
    return [m for m in KNOWN_LIVE_MODELS if cur.get(m) == "live"]


# All four models store their trades under strategy_bundle::<model>.
def _bundle_key(model):
    return ("strategy_bundle::" + model) if model in KNOWN_LIVE_MODELS else None


def archive_and_reset_model(model, phase="paper", reason="go_live"):
    """Move a model's CURRENT trades into its `archive` list (tagged with phase +
    timestamp) and reset `trades` to empty. NON-DESTRUCTIVE — nothing is deleted,
    only moved. Called when a model goes live so live trading starts fresh from
    the ORIGINAL configured capital; paper history is preserved in `archive`."""
    key = _bundle_key(model)
    if not key:
        return 0
    data = kv_get(key, {}) or {}
    trades = data.get("trades", []) or []
    if not trades:
        return 0
    archive = data.get("archive", []) or []
    stamp = now_utc_iso()
    for t in trades:
        rec = dict(t) if isinstance(t, dict) else {"value": t}
        rec.setdefault("phase", phase)
        rec["archived_at"] = stamp
        rec["archived_reason"] = reason
        archive.append(rec)
    data["archive"] = archive
    data["trades"] = []
    kv_set(key, data)
    log_automation(
        f"{model}: archived {len(trades)} {phase} trade(s) [{reason}] — live starts fresh from original capital",
        level="INFO")
    return len(trades)


def _bundle_key_for(model, user_id=None):
    """Explicit strategy-bundle key. Per-user when user_id given, else global.
    Unlike read_bundle_key(), never consults the session — safe for writes."""
    if user_id:
        return "strategy_bundle::%s::%s" % (user_id, model)
    return _bundle_key(model)


def _infer_active_phase(trades):
    """Best-effort phase of the trades currently sitting in `trades`.
    A live trade carries a live_* marker from _live_entry; paper trades don't."""
    if not trades:
        return None
    for t in trades:
        if isinstance(t, dict) and (t.get("live_status") or t.get("live_order_id")
                                    or t.get("live_entry_order_id") or t.get("live_orders")):
            return "live"
    return "paper"


def apply_phase_transition(model, prev_mode, new_mode, user_id=None):
    """Two-way, non-destructive swap of a model's trade data on a mode change.

    Paper and live trades are kept in separate stashes. Whichever phase is active
    lives in `trades`; the other waits in `phase_stash`. On:
      paper -> live : stash the paper trades, restore (empty) live trades  = go-live archive
      live  -> paper: stash the live trades, restore the paper trades      = fallback rollback
      * -> off      : stash the active phase, show nothing
      off -> paper/live: restore that phase's stash
    Nothing is ever deleted — switching back always brings the trades back.
    """
    def phase_of(m):
        return "live" if m == "live" else ("paper" if m == "paper" else None)
    new_ph = phase_of(new_mode)
    key = _bundle_key_for(model, user_id)
    if not key:
        return
    data = kv_get(key, {}) or {}
    trades = data.get("trades", []) or []
    # The phase the CURRENT active trades belong to. Prefer the tracked value;
    # fall back to the previous mode, then to inferring from trade content
    # (handles bundles created before this feature existed).
    cur_ph = data.get("active_phase")
    if cur_ph is None:
        # No tracked phase yet (bundle predates this feature, or state was left
        # inconsistent). Trust the trade CONTENT first — a live trade carries a
        # live_* marker — and only fall back to the previous mode when there are
        # no trades to infer from.
        cur_ph = _infer_active_phase(trades)
        if cur_ph is None:
            cur_ph = phase_of(prev_mode)
    if cur_ph == new_ph:
        # No phase change (e.g. paper->off->paper with nothing between, or the
        # active trades already match the target). Just keep the tag accurate.
        data["active_phase"] = new_ph
        kv_set(key, data)
        return
    stash = data.get("phase_stash") or {"paper": [], "live": []}
    # Set the outgoing phase's trades aside.
    if cur_ph is not None:
        stash[cur_ph] = trades
    # Bring the incoming phase's trades back (off = nothing active).
    if new_ph is not None:
        data["trades"] = list(stash.get(new_ph, []) or [])
        stash[new_ph] = []
    else:
        data["trades"] = []
    data["phase_stash"] = stash
    data["active_phase"] = new_ph
    kv_set(key, data)
    log_automation(
        f"{model}[{user_id or 'instance'}]: phase {prev_mode}->{new_mode} — "
        f"stashed {len(trades) if cur_ph else 0} {cur_ph or '-'} trade(s), "
        f"restored {len(data['trades'])} {new_ph or '-'} trade(s)",
        level="INFO")


def _model_from_reason(reason):
    """Map an order reason string to its originating model. Fail-closed:
    returns None if it can't be positively identified."""
    r = str(reason or "").lower()
    if r.startswith("futures"):
        return "futures"
    for m in ("nifty_strangle_w", "nexp_workstation", "ob_workstation", "ait_workstation"):
        if m in r:
            return m
    return None

def live_order_permitted(reason, user_id=None):
    """True only if the order's model is currently enabled for live trading
    (for this user, if user_id given)."""
    model = _model_from_reason(reason)
    if not model:
        return False
    return model in get_live_enabled_models(user_id)


def multi_tenant_enabled():
    """Master flag for per-user execution (Phase 3). OFF by default — everything
    runs on the single global account until this is turned on."""
    return kv_get("multi_tenant_enabled", False) is True or os.environ.get("MULTI_TENANT") == "1"


def read_bundle_key(model, user_id=None):
    """Which strategy_bundle key to READ for a page. In multi-tenant mode, the
    logged-in user's own bundle (so each user sees only their data); otherwise global."""
    uid = user_id
    if uid is None and multi_tenant_enabled():
        uid = session.get("user_id")
    return ("strategy_bundle::%s::%s" % (uid, model)) if uid else ("strategy_bundle::" + model)


def paper_phase_archive(data):
    """Paper-phase trades to show in the dashboard's PAPER row. When a model is
    live its paper history lives in phase_stash['paper']; otherwise fall back to
    the legacy `archive`. Ensures paper history stays visible after go-live."""
    if isinstance(data, dict) and data.get("active_phase") == "live":
        stash = data.get("phase_stash") or {}
        paper = stash.get("paper") or []
        if paper:
            return paper
    return (data.get("archive", []) or []) if isinstance(data, dict) else []


def _futures_bundle_key(user_id=None):
    return ("strategy_bundle::%s::futures" % user_id) if user_id else "strategy_bundle::futures"


def _pos_cache_key(user_id=None):
    return ("dry_run_module_positions::%s" % user_id) if user_id else "dry_run_module_positions"


# ── Strategy factory (plug-and-play instances) ───────────────────────────────
# A "strategy" is a named instance of an existing engine type (futures/ob/ait/
# nexp). Each has its own isolated paper book. Adding one is zero-code; the
# engine is reused via instance-scoped bundle keys (Phase 3b wires the signal).
STRATEGY_TYPES = {"futures", "ob_workstation", "ait_workstation", "nexp_workstation"}


def get_strategies():
    return kv_get("strategies", []) or []


def strategy_bundle_key(sid):
    return "strategy_bundle::inst::%s" % sid


def _can_trade_user(user):
    if not user:
        return False
    if user.get("is_admin"):
        return True
    access = TIER_ACCESS.get(user.get("tier", "tier1"), [])
    return bool({"futures", "automation"} & set(access))


@app.route("/api/strategies", methods=["GET", "POST"])
def api_strategies():
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    if request.method == "POST":
        user = get_user_by_id(session["user_id"])
        if not _can_trade_user(user):
            return jsonify({"ok": False, "error": "your role cannot add strategies"}), 403
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        stype = body.get("type")
        if not name or stype not in STRATEGY_TYPES:
            return jsonify({"ok": False, "error": "a name and a valid type are required"}), 400
        import uuid as _uuid
        sid = _uuid.uuid4().hex[:10]
        rec = {"id": sid, "slug": sid, "name": name, "type": stype, "params": body.get("params") or {},
               "owner": session["user_id"], "created_at": now_utc_iso(),
               "webhook": body.get("webhook") or "new", "paper_active": False, "mode": "off"}
        strategies = get_strategies()
        strategies.append(rec)
        kv_set("strategies", strategies)
        kv_set(strategy_bundle_key(sid), {"trades": [], "archive": [], "config": rec["params"]})
        log_automation(f"Strategy added: {name} ({stype}) [{sid}]", level="INFO")
        return jsonify({"ok": True, "strategy": rec})
    return jsonify({"ok": True, "strategies": get_strategies(),
                    "types": sorted(STRATEGY_TYPES),
                    "can_add": _can_trade_user(get_user_by_id(session["user_id"]))})


@app.route("/api/strategies/<sid>", methods=["DELETE"])
def api_strategy_delete(sid):
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    if not _can_trade_user(get_user_by_id(session["user_id"])):
        return jsonify({"ok": False, "error": "your role cannot remove strategies"}), 403
    strategies = [s for s in get_strategies() if s.get("id") != sid]
    kv_set("strategies", strategies)
    log_automation(f"Strategy removed: {sid}", level="INFO")
    return jsonify({"ok": True})


def _place_order_direct(transaction_type, symbol, quantity, reason="strategy", user_id=None):
    """Place a REAL order WITHOUT the per-model guard. Used ONLY for LIVE strategy
    instances — the instance's explicit LIVE mode (default OFF) is the gate."""
    try:
        kite = get_kite(require_token=True, user_id=user_id)
        oid = kite.place_order(
            variety="regular", exchange="NFO", tradingsymbol=symbol,
            transaction_type=transaction_type, quantity=int(quantity),
            product=AUTOMATION_PRODUCT, order_type=AUTOMATION_ORDER_TYPE,
            market_protection=AUTOMATION_MARKET_PROTECTION,
        )
        log_automation(f"LIVE ORDER (strategy) {transaction_type} {symbol} x{quantity} id={oid} [{reason}]",
                       details={"order_id": oid, "reason": reason})
        try:
            send_telegram(f"🟢 <b>LIVE (strategy)</b> {transaction_type} {symbol} x{quantity}\n{reason}")
        except Exception:
            pass
        return {"ok": True, "order_id": oid}
    except Exception as e:
        log_automation(f"LIVE ORDER (strategy) FAILED {transaction_type} {symbol} x{quantity}: {e} [{reason}]", level="ERROR")
        return {"ok": False, "error": str(e)}


class _StrategyScopedApp:
    """Runs an existing engine against ONE strategy instance's isolated PAPER book.
    Rewrites the model's storage key to the instance bundle and forces paper mode
    (no live orders). The engine code (models_v2) is reused entirely unchanged —
    same trick as _UserScopedApp, keyed to a strategy instance instead of a user."""
    def __init__(self, app_module, instance):
        self._app = app_module
        self._id = instance["id"]
        self._type = instance["type"]
        self._mode = instance.get("mode", "off")   # off / paper / live (default off)
        self._owner = instance.get("owner")
        self._storage = "strategy_bundle::" + instance["type"]
        self._instkey = strategy_bundle_key(self._id)

    def _rk(self, key):
        if key == self._storage:
            return self._instkey
        if key == "dry_run_module_positions":
            return "dry_run_module_positions::inst::%s" % self._id
        return key

    def kv_get(self, key, default=None):
        return self._app.kv_get(self._rk(key), default)

    def kv_set(self, key, value):
        return self._app.kv_set(self._rk(key), value)

    def read_bundle_key(self, model, user_id=None):
        return self._instkey

    def get_model_mode(self, model, user_id=None):
        return self._mode                    # instance's own off/paper/live gate

    def get_live_enabled_models(self, user_id=None):
        return [self._type] if self._mode == "live" else []

    def place_live_order_with_retry(self, transaction_type, symbol, quantity,
                                    reason="automation", max_retries=3, user_id=None):
        # Only a LIVE instance places real orders — its explicit mode (default off)
        # IS the authorization, so route around the per-model guard using a direct
        # order on the OWNER's token. Paper/off just log.
        if self._mode == "live":
            return _place_order_direct(transaction_type, symbol, quantity,
                                       reason="strat::%s::%s" % (self._id, reason),
                                       user_id=self._owner)
        self._app.log_automation(f"PAPER [strategy {self._id}] {transaction_type} {symbol} x{quantity} [{reason}]")
        return {"ok": True, "dry_run": True, "order_id": "DRYRUN-%d" % int(time.time())}

    def send_telegram(self, message, chat_id=None):
        # Strategy alerts go to the strategy owner's own Telegram (fallback: global).
        cid = chat_id or self._app.get_user_telegram_chat(self._owner)
        return self._app.send_telegram(message, chat_id=cid)

    def __getattr__(self, name):
        return getattr(self._app, name)


@app.route("/api/strategy/<sid>/webhook", methods=["POST"])
def api_strategy_webhook(sid):
    """Per-strategy TradingView webhook — records a PAPER trade into the instance's
    own book via its engine type. Never places real orders."""
    try:
        payload = parse_tradingview_payload()
        secret = str(payload.get("secret") or payload.get("token") or "").strip()
        if TRADINGVIEW_WEBHOOK_SECRET and secret != TRADINGVIEW_WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "invalid_secret"}), 403
        inst = next((s for s in get_strategies() if s.get("id") == sid or s.get("slug") == sid), None)
        if not inst:
            return jsonify({"ok": False, "error": "unknown_strategy"}), 404
        signal = normalize_signal(payload.get("signal") or payload.get("trend") or payload.get("side"))
        spot_raw = payload.get("close", payload.get("spot", payload.get("price")))
        if spot_raw is None:
            return jsonify({"ok": False, "error": "missing_close"}), 400
        spot = float(spot_raw)
        stype = inst.get("type")
        if stype not in ("ob_workstation", "ait_workstation", "nexp_workstation"):
            return jsonify({"ok": False, "error": "futures-type instances aren't signal-driven via webhook yet"}), 400
        import sys as _sys
        scoped = _StrategyScopedApp(_sys.modules[__name__], inst)
        models_v2._process_model_signal(scoped, stype, signal, spot, now_utc_iso())
        log_automation(f"Strategy '{inst.get('name')}' [{sid}] PAPER signal {signal} @ {spot}", level="INFO")
        return jsonify({"ok": True, "signal": signal, "spot": spot, "strategy": inst.get("name")})
    except Exception as e:
        log_automation(f"Strategy webhook error [{sid}]: {e}", level="ERROR")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/strategy/<sid>", methods=["GET"])
def api_strategy_data(sid):
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    inst = next((s for s in get_strategies() if s.get("id") == sid), None)
    if not inst:
        return jsonify({"ok": False, "error": "unknown_strategy"}), 404
    bundle = kv_get(strategy_bundle_key(sid), {}) or {}
    trades = bundle.get("trades", []) or []
    cfg = bundle.get("config", {}) or {}
    stype = inst.get("type")
    try:
        if stype in ("ait_workstation", "nexp_workstation"):
            results, summary = calc_spread_trades(trades, cfg)
        elif stype == "futures":
            results, summary = calc_futures_trades(trades, cfg)
        else:
            results, summary = trades, {}
    except Exception:
        results, summary = trades, {}
    return jsonify({"ok": True, "strategy": inst, "trades": results, "summary": summary, "config": cfg})


@app.route("/api/strategies/<sid>/mode", methods=["POST"])
def api_strategy_mode(sid):
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    if not _can_trade_user(get_user_by_id(session["user_id"])):
        return jsonify({"ok": False, "error": "your role cannot change strategy modes"}), 403
    body = request.get_json(force=True) or {}
    mode = body.get("mode")
    if mode not in ("off", "paper", "live"):
        return jsonify({"ok": False, "error": "mode must be off/paper/live"}), 400
    strategies = get_strategies()
    found = None
    for s in strategies:
        if s.get("id") == sid:
            found = s
            s["mode"] = mode
            s["paper_active"] = (mode != "off")
            break
    if not found:
        return jsonify({"ok": False, "error": "unknown_strategy"}), 404
    kv_set("strategies", strategies)
    log_automation(f"Strategy [{sid}] mode -> {mode}", level="INFO")
    return jsonify({"ok": True, "strategy": found})


@app.route("/api/strategies/<sid>/config", methods=["POST"])
def api_strategy_config(sid):
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    if not _can_trade_user(get_user_by_id(session["user_id"])):
        return jsonify({"ok": False, "error": "your role cannot edit strategies"}), 403
    body = request.get_json(force=True) or {}
    slug = "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in str(body.get("slug", "")).strip().lower()).strip("-")
    strategies = get_strategies()
    if slug and any(s.get("slug") == slug and s.get("id") != sid for s in strategies):
        return jsonify({"ok": False, "error": "that webhook id is already taken"}), 400
    found = None
    for s in strategies:
        if s.get("id") == sid:
            found = s
            if slug:
                s["slug"] = slug
            if body.get("webhook") in ("new", "existing"):
                s["webhook"] = body["webhook"]
            break
    if not found:
        return jsonify({"ok": False, "error": "unknown_strategy"}), 404
    cfg = body.get("config")
    if isinstance(cfg, dict):
        bkey = strategy_bundle_key(found["id"])
        bundle = kv_get(bkey, {}) or {}
        bundle["config"] = {**(bundle.get("config") or {}), **cfg}
        kv_set(bkey, bundle)
        found["params"] = {**(found.get("params") or {}), **cfg}
    kv_set("strategies", strategies)
    log_automation(f"Strategy [{sid}] config updated", level="INFO")
    return jsonify({"ok": True, "strategy": found})


@app.route("/api/strategies/<sid>/signal", methods=["POST"])
def api_strategy_manual_signal(sid):
    """Authed manual Add-Trade for a strategy instance. Fires a signal through the
    SAME engine (_process_model_signal) on the instance's isolated book — no
    TradingView secret required — so a hand-entered trade is identical to a
    webhook-driven one. Never places real orders (scoped app forces paper unless
    the instance itself is LIVE, same rule as the webhook)."""
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    if not _can_trade_user(get_user_by_id(session["user_id"])):
        return jsonify({"ok": False, "error": "your role cannot trade strategies"}), 403
    inst = next((s for s in get_strategies() if s.get("id") == sid or s.get("slug") == sid), None)
    if not inst:
        return jsonify({"ok": False, "error": "unknown_strategy"}), 404
    body = request.get_json(force=True) or {}
    signal = normalize_signal(body.get("signal") or body.get("trend") or body.get("side"))
    spot_raw = body.get("spot", body.get("close", body.get("price")))
    if spot_raw in (None, ""):
        return jsonify({"ok": False, "error": "spot is required"}), 400
    try:
        spot = float(spot_raw)
    except Exception:
        return jsonify({"ok": False, "error": "spot must be a number"}), 400
    stype = inst.get("type")
    if stype not in ("ob_workstation", "ait_workstation", "nexp_workstation"):
        return jsonify({"ok": False, "error": "futures-type instances aren't signal-driven yet"}), 400
    when = (str(body.get("date") or "").strip()) or now_utc_iso()
    import sys as _sys
    scoped = _StrategyScopedApp(_sys.modules[__name__], inst)
    models_v2._process_model_signal(scoped, stype, signal, spot, when)
    log_automation(f"Strategy '{inst.get('name')}' [{sid}] MANUAL signal {signal} @ {spot}", level="INFO")
    return jsonify({"ok": True, "signal": signal, "spot": spot})


@app.route("/api/admin/multi_tenant", methods=["GET", "POST"])
def api_multi_tenant():
    """Master flag for Phase-3 per-user execution. Admin-only to change.
    OFF = single global account (current behaviour). ON = per-user engine."""
    if request.method == "POST":
        if not session.get("is_admin"):
            return jsonify({"ok": False, "error": "admin required"}), 403
        body = request.get_json(force=True) or {}
        enabled = bool(body.get("enabled"))
        kv_set("multi_tenant_enabled", enabled)
        log_automation(f"MULTI_TENANT set to {enabled}", level="INFO")
        return jsonify({"ok": True, "multi_tenant_enabled": enabled})
    return jsonify({"ok": True, "multi_tenant_enabled": multi_tenant_enabled(),
                    "active_trading_users": len(active_trading_users())})


def active_trading_users(model=None):
    """App user_ids that have connected their own Zerodha token AND (for `model`,
    if given) are not 'off'. Used to fan the engine out per user in Phase 3."""
    out = []
    try:
        for u in get_all_users():
            uid = u.get("id")
            if not uid:
                continue
            if not get_access_token(uid):
                continue
            if model and get_model_mode(model, uid) == "off":
                continue
            out.append(uid)
    except Exception as e:
        log_automation(f"active_trading_users error: {e}", level="WARNING")
    return out


class _UserScopedApp:
    """Wraps the app module so models_v2 (and any handler) operates on ONE user's
    data + token + modes, with NO changes to models_v2 itself. Strategy-bundle and
    position-cache keys are rewritten to per-user; token/modes/orders are scoped."""
    def __init__(self, app_module, user_id):
        self._app = app_module
        self._uid = user_id

    def _rk(self, key):
        if isinstance(key, str):
            if key.startswith("strategy_bundle::") and not key.startswith("strategy_bundle::%s::" % self._uid):
                model = key.split("strategy_bundle::", 1)[1]
                return "strategy_bundle::%s::%s" % (self._uid, model)
            if key in ("dry_run_module_positions", "workstation_positions"):
                return "%s::%s" % (key, self._uid)
        return key

    def kv_get(self, key, default=None):
        return self._app.kv_get(self._rk(key), default)

    def kv_set(self, key, value):
        return self._app.kv_set(self._rk(key), value)

    def get_kite(self, require_token=True):
        return self._app.get_kite(require_token=require_token, user_id=self._uid)

    def get_access_token(self, user_id=None):
        return self._app.get_access_token(self._uid)

    def get_model_mode(self, model, user_id=None):
        return self._app.get_model_mode(model, self._uid)

    def get_live_enabled_models(self, user_id=None):
        return self._app.get_live_enabled_models(self._uid)

    def place_live_order_with_retry(self, transaction_type, symbol, quantity, reason="automation", max_retries=3, user_id=None):
        return self._app.place_live_order_with_retry(transaction_type, symbol, quantity,
                                                     reason=reason, max_retries=max_retries, user_id=self._uid)

    def send_telegram(self, message, chat_id=None):
        # Route this user's alerts to THEIR own Telegram chat (fallback: global).
        cid = chat_id or self._app.get_user_telegram_chat(self._uid)
        return self._app.send_telegram(message, chat_id=cid)

    def __getattr__(self, name):
        # Everything else (log_automation, send_telegram, quote helpers, master_state,
        # constants, etc.) delegates to the real app module unchanged.
        return getattr(self._app, name)

def _alert_live_blocked(transaction_type, symbol, quantity, reason):
    _model = _model_from_reason(reason)
    log_automation(
        f"LIVE BLOCKED {transaction_type} {symbol} x{quantity} [{reason}] "
        f"— model '{_model}' not in live_enabled_models={get_live_enabled_models()}", level="ERROR")
    try:
        send_telegram(
            f"⛔ <b>LIVE ORDER BLOCKED</b>\n{transaction_type} {symbol} x{quantity}\n"
            f"reason: {reason}\nModel '{_model}' is not live-enabled. Treated as paper.")
    except Exception:
        pass


def place_order(transaction_type, symbol, quantity, reason='automation'):
    payload = create_order_payload(symbol, transaction_type, quantity)
    payload['reason'] = reason
    # Sole gate: the model must be live-enabled (see live_enabled_models / go-live board)
    if not live_order_permitted(reason):
        log_automation(f"PAPER {transaction_type} {symbol} x{quantity} (model not live-enabled)", details=payload)
        return {'ok': True, 'dry_run': True, 'order_id': f"DRYRUN-{int(time.time())}", 'payload': payload}
    kite = get_kite(require_token=True)
    order_id = kite.place_order(
        variety='regular', exchange='NFO', tradingsymbol=symbol,
        transaction_type=transaction_type, quantity=int(quantity),
        product=AUTOMATION_PRODUCT, order_type=AUTOMATION_ORDER_TYPE,
        market_protection=AUTOMATION_MARKET_PROTECTION,
    )
    log_automation(f"LIVE {transaction_type} {symbol} x{quantity}", details={'order_id': order_id, **payload})
    return {'ok': True, 'dry_run': False, 'order_id': order_id, 'payload': payload}







automation_state = {
    'kill_switch': False,
    'automation_enabled': False,
}

# ─── In-memory data store ───────────────────────────────────────────────────
# Each sheet's config is stored here and recalculated on every write.

def mround(value, multiple):
    if multiple == 0:
        return value
    return round(round(value / multiple) * multiple, 10)

def txn_cost_rate():
    return 0.004  # Master!L3 default

# ──────────────────────────────────────────────────────────────────────────────
# MASTER sheet state
# ──────────────────────────────────────────────────────────────────────────────
master_state = {
    "nifty_spot": 22545.05,
    "nifty_trade_date": date.today().isoformat(),
    "nifty_trend": "LONG",
    "nifty_trade_level": 17319,
    "nifty_lot_size": CURRENT_NIFTY_LOT_SIZE,
    "cost_buy_per_lot": None,  # computed
    "cost_sell_per_lot": None,  # computed
    "txn_cost": 0.004,
    "signal_source": "MANUAL",
    "signal_time": None,
}

# ──────────────────────────────────────────────────────────────────────────────
# Generic trade-list sheets
# ──────────────────────────────────────────────────────────────────────────────
# Futures Model
futures_config = {
    "capital": 897156.01,
    "nifty_start": 17277,
    "leverage": 2,
    "lot_size": CURRENT_NIFTY_LOT_SIZE,
    "txn_cost": 0.004,
    "signal_source": "MANUAL",
    "signal_time": None,
}

futures_trades = []

# Options Buy

opt_buy_trades = []

# Options AIT

opt_ait_trades = []

# Nifty EXP

nifty_exp_trades = []

# NIFTY EXP WORKSTATION — windowed weekly spread on workstation SuperTrend ATR 10/3 direction
nexp_workstation_config = {
    "capital": 500000,
    "risk_factor": 0.12,
    "lot_size": CURRENT_NIFTY_LOT_SIZE,
    "strike_gap": 100,
    "txn_cost": 0.004,
    "signal_source": "WORKSTATION_PREVAILING",
    "signal_time": None,
}

# FINNIFTY EXP
finnifty_exp_config = {
    "capital": 500000,
    "risk_factor": 0.12,
    "risk_tolerance": 0.20,
    "lot_size": 40,
    "strike_gap": 100,
    "txn_cost": 0.004,
    "signal_source": "MANUAL",
    "signal_time": None,
}

finnifty_exp_trades = [
    {"date":"2022-01-03","spot":17758,"expiry":"2022-01-04","trend":"LONG","atm_sell_price":128,"atm_exit_price":0,"otm_buy_price":85,"otm_exit_price":0},
    {"date":"2022-01-10","spot":18490,"expiry":"2022-01-11","trend":"LONG","atm_sell_price":85,"atm_exit_price":0,"otm_buy_price":47,"otm_exit_price":0},
]

# STAIRS EXP LR / HR  (same structure, diff risk factor)
stairs_lr_config = {"capital":500000,"risk_factor":0.04,"nifty_lot":CURRENT_NIFTY_LOT_SIZE,"finnifty_lot":40,"strike_gap":100,"txn_cost":0.004}
stairs_hr_config = {"capital":500000,"risk_factor":0.08,"nifty_lot":CURRENT_NIFTY_LOT_SIZE,"finnifty_lot":40,"strike_gap":100,"txn_cost":0.004}

stairs_lr_trades = [
    {"date":"2022-01-03","instrument":"FINNIFTY","spot":17758,"expiry":"2022-01-04","trend":"LONG","atm_sell_price":128,"atm_exit_price":0,"otm_buy_price":85,"otm_exit_price":0,"exit_date":"2022-01-04"},
    {"date":"2022-01-05","instrument":"NIFTY","spot":17936,"expiry":"2022-01-06","trend":"LONG","atm_sell_price":39,"atm_exit_price":165,"otm_buy_price":14,"otm_exit_price":56,"exit_date":"2022-01-06"},
]

stairs_hr_trades = list(stairs_lr_trades)  # same seed data

# Payoff
payoff_state = {
    "index": "Nifty",
    "spot": 17211,
    "signal": "LONG",
    "strike_gap": 100,
    "atm_sell_strike": 17200,
    "atm_sell_premium": 128,
    "otm_buy_strike": 17100,
    "otm_buy_premium": 93,
    "quantity": 500,
    "capital": 500000,
}



# ──────────────────────────────────────────────────────────────────────────────
# Zerodha / Kite Connect helpers
# ──────────────────────────────────────────────────────────────────────────────
instrument_cache = {
    "loaded_at": None,
    "refreshed_at": None,
    "rows": [],
}

def zerodha_ready(user_id=None):
    if not KiteConnect:
        return False
    api_key, api_secret = resolve_kite_creds(user_id)
    return bool(api_key and api_secret)

def get_kite(require_token=True, user_id=None):
    if KiteConnect is None:
        raise RuntimeError("kiteconnect package not installed. Run: pip install kiteconnect")
    api_key, api_secret = resolve_kite_creds(user_id)
    if not api_key or not api_secret:
        raise RuntimeError("Missing Kite API key/secret. Add your Zerodha API credentials first.")
    kite = KiteConnect(api_key=api_key)
    if require_token:
        # With user_id -> that user's own token only (no cross-account fallback).
        # Without -> the instance/global token (engine), with session fallback.
        if user_id:
            access_token = get_access_token(user_id)
        else:
            access_token = get_access_token() or session.get("kite_access_token")
        if not access_token:
            raise RuntimeError("Zerodha access token missing. Please connect Zerodha first.")
        kite.set_access_token(access_token)
    return kite

def round_to_100(value):
    return int(round(float(value) / 100.0) * 100)

def refresh_nfo_instruments(force=False):
    today = date.today()
    loaded_at = instrument_cache.get("loaded_at")
    if instrument_cache.get("rows") and loaded_at == today and not force:
        return instrument_cache["rows"]
    kite = get_kite(require_token=True)
    rows = kite.instruments("NFO")
    instrument_cache["rows"] = rows
    instrument_cache["loaded_at"] = today
    instrument_cache["refreshed_at"] = now_utc_iso()
    return rows

def get_nfo_instruments():
    return refresh_nfo_instruments(force=False)

def spread_option_type(signal):
    """AIT sell spread: LONG→sell PE spread, SHORT→sell CE spread."""
    signal = str(signal).upper()
    if signal == "LONG":
        return "PE"
    if signal == "SHORT":
        return "CE"
    raise RuntimeError("signal must be LONG or SHORT")

def buy_option_type(signal):
    """Options Buy: LONG→buy CE, SHORT→buy PE."""
    signal = str(signal).upper()
    if signal == "LONG":
        return "CE"
    if signal == "SHORT":
        return "PE"
    raise RuntimeError("signal must be LONG or SHORT")


def pick_nearest_nifty_expiry(signal, spot):
    strike = round_to_100(spot)
    opt_type = spread_option_type(signal)
    rows = get_nfo_instruments()
    expiries = sorted({row.get("expiry") for row in rows if row.get("name") == "NIFTY" and row.get("segment") == "NFO-OPT" and row.get("instrument_type") == opt_type and int(float(row.get("strike", 0) or 0)) == strike and row.get("expiry") and row.get("expiry") >= date.today()})
    if not expiries:
        raise RuntimeError(f"No future NIFTY expiry found for strike {strike} {opt_type}")
    expiry = expiries[0]
    return expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)

def pick_nifty_option_contract_by_type(opt_type, spot, expiry_yyyy_mm_dd):
    """Pick NIFTY option contract by explicit option type (CE or PE)."""
    strike = round_to_100(spot)
    expiry_date = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d").date() if expiry_yyyy_mm_dd else None
    rows = get_nfo_instruments()
    for row in rows:
        expiry = row.get("expiry")
        if (
            row.get("name") == "NIFTY"
            and row.get("segment") == "NFO-OPT"
            and row.get("instrument_type") == opt_type
            and int(float(row.get("strike", 0) or 0)) == strike
            and expiry == expiry_date
        ):
            return {
                "exchange": row["exchange"],
                "tradingsymbol": row["tradingsymbol"],
                "instrument_token": row["instrument_token"],
                "strike": strike,
                "option_type": opt_type,
                "expiry": expiry_yyyy_mm_dd,
            }
    raise RuntimeError(f"No NIFTY {opt_type} option found for strike {strike} expiry {expiry_yyyy_mm_dd}")


def pick_nifty_option_contract_at_strike(opt_type, strike, expiry_yyyy_mm_dd):
    """Pick a NIFTY option contract at an EXACT strike the caller has already
    computed (e.g. an OTM % offset from spot) — unlike pick_nifty_option_contract*
    above, which always derives the ATM strike from spot itself."""
    if not expiry_yyyy_mm_dd:
        raise RuntimeError("Expiry date is required but was empty.")
    expiry_date = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d").date()
    rows = get_nfo_instruments()
    for row in rows:
        if (
            row.get("name") == "NIFTY"
            and row.get("segment") == "NFO-OPT"
            and row.get("instrument_type") == opt_type
            and int(float(row.get("strike", 0) or 0)) == int(strike)
            and row.get("expiry") == expiry_date
        ):
            return {
                "exchange": row["exchange"],
                "tradingsymbol": row["tradingsymbol"],
                "instrument_token": row["instrument_token"],
                "strike": int(strike),
                "option_type": opt_type,
                "expiry": expiry_yyyy_mm_dd,
            }
    raise RuntimeError(f"No NIFTY {opt_type} option found for strike {strike} expiry {expiry_yyyy_mm_dd}")


def pick_nifty_option_contract(signal, spot, expiry_yyyy_mm_dd=None):
    strike = round_to_100(spot)
    opt_type = spread_option_type(signal)
    if not expiry_yyyy_mm_dd:
        raise RuntimeError("Expiry date is required but was empty. Please ensure expiry is set before fetching premiums.")
    expiry_date = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d").date()
    rows = get_nfo_instruments()
    matches = []
    for row in rows:
        expiry = row.get("expiry")
        if (
            row.get("name") == "NIFTY"
            and row.get("segment") == "NFO-OPT"
            and row.get("instrument_type") == opt_type
            and int(float(row.get("strike", 0) or 0)) == strike
            and expiry == expiry_date
        ):
            matches.append(row)
    if not matches:
        raise RuntimeError(f"No NIFTY option found for strike {strike} {opt_type} expiry {expiry_yyyy_mm_dd}")
    row = matches[0]
    return {
        "exchange": row["exchange"],
        "tradingsymbol": row["tradingsymbol"],
        "instrument_token": row["instrument_token"],
        "strike": strike,
        "option_type": opt_type,
        "expiry": expiry_yyyy_mm_dd,
    }


def pick_nifty_futures_contract(use_rollover_logic=False):
    """
    Pick the appropriate NIFTY futures contract.
    use_rollover_logic=True: if nearest expiry is within 7 days, use next month instead.
    This prevents entering a near-expiry contract on signal reversal.
    """
    from datetime import timedelta
    rows = get_nfo_instruments()
    today = date.today()
    matches = []
    for row in rows:
        expiry = row.get("expiry")
        if row.get("name") == "NIFTY" and row.get("segment") == "NFO-FUT" and expiry and expiry >= today:
            matches.append(row)
    if not matches:
        raise RuntimeError("No live NIFTY futures contract found")
    matches.sort(key=lambda r: r.get("expiry"))

    # Rollover logic: if signal reversal and nearest expiry <= 7 days away,
    # use next month contract to avoid entering a near-expiry position
    if use_rollover_logic and len(matches) > 1:
        nearest_expiry = matches[0].get("expiry")
        days_to_expiry = (nearest_expiry - today).days
        if days_to_expiry <= 7:
            log_automation(
                f"Futures rollover: nearest expiry {nearest_expiry} is {days_to_expiry} days away "
                f"— using next month contract {matches[1].get('expiry')}",
                level="INFO"
            )
            row = matches[1]
        else:
            row = matches[0]
    else:
        row = matches[0]

    return {
        "exchange": row["exchange"],
        "tradingsymbol": row["tradingsymbol"],
        "instrument_token": row["instrument_token"],
        "expiry": row["expiry"].isoformat() if hasattr(row["expiry"], "isoformat") else str(row["expiry"]),
        "lot_size": int(row.get("lot_size") or CURRENT_NIFTY_LOT_SIZE),
    }

def get_nifty_spread_quote(signal, spot, expiry_yyyy_mm_dd=None, strike_gap=100):
    signal = str(signal).upper()
    if signal not in ("LONG", "SHORT"):
        raise RuntimeError("signal must be LONG or SHORT")
    if not expiry_yyyy_mm_dd:
        expiry_yyyy_mm_dd = pick_nearest_nifty_expiry(signal, spot)
    atm_contract = pick_nifty_option_contract(signal, spot, expiry_yyyy_mm_dd)
    atm_strike = int(atm_contract["strike"])
    otm_strike = atm_strike - int(strike_gap) if signal == "LONG" else atm_strike + int(strike_gap)
    opt_type = atm_contract["option_type"]
    if not expiry_yyyy_mm_dd:
        raise RuntimeError("Expiry date is required but was empty.")
    expiry_date = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d").date()
    rows = get_nfo_instruments()
    otm_match = None
    for row in rows:
        expiry = row.get("expiry")
        if (
            row.get("name") == "NIFTY"
            and row.get("segment") == "NFO-OPT"
            and row.get("instrument_type") == opt_type
            and int(float(row.get("strike", 0) or 0)) == int(otm_strike)
            and expiry == expiry_date
        ):
            otm_match = row
            break
    if not otm_match:
        raise RuntimeError(f"No NIFTY option found for strike {otm_strike} {opt_type} expiry {expiry_yyyy_mm_dd}")
    kite = get_kite(require_token=True)
    keys = [f"{atm_contract['exchange']}:{atm_contract['tradingsymbol']}", f"{otm_match['exchange']}:{otm_match['tradingsymbol']}"]
    q = kite.quote(keys)
    atm_data = q[keys[0]]
    otm_data = q[keys[1]]
    return {
        "signal": signal,
        "spot": float(spot),
        "expiry": expiry_yyyy_mm_dd,
        "atm_strike": atm_strike,
        "otm_strike": int(otm_strike),
        "option_type": opt_type,
        "atm_tradingsymbol": atm_contract['tradingsymbol'],
        "otm_tradingsymbol": otm_match['tradingsymbol'],
        "atm_sell_premium": float(atm_data['last_price']),
        "otm_buy_premium": float(otm_data['last_price']),
        "quote_time": now_utc_iso(),
        "instruments_refreshed_at": instrument_cache.get("refreshed_at"),
    }

def _to_ist_display(ts):
    if not ts:
        return ts
    try:
        if isinstance(ts, str):
            cleaned = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
        else:
            dt = ts
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return ts

def _load_module_bundle(name, cfg_obj, trades_name):
    data = kv_get(f"strategy_bundle::{name}", None)
    if isinstance(data, dict):
        cfg = data.get("config")
        trades = data.get("trades")
        if isinstance(cfg, dict):
            cfg_obj.update(cfg)
        if isinstance(trades, list):
            target = globals()[trades_name]
            # Only reload from DB if DB has MORE trades than memory
            # This prevents a worker with stale empty memory from wiping good data
            if len(trades) >= len(target):
                target.clear()
                target.extend(trades)

def _refresh_module_state_from_db():
    _load_module_bundle("futures", futures_config, "futures_trades")
    # legacy models (options_buy / options_ait / nifty_exp) retired 2026-07-24

def _persist_module_state_all():
    kv_set("strategy_bundle::futures", {"config": futures_config, "trades": globals()["futures_trades"]})
    # legacy models (options_buy / options_ait / nifty_exp) retired 2026-07-24

def _append_trade_to_bundle(bundle_name, trade):
    """Atomically append a trade to a strategy bundle in DB - bypasses in-memory race conditions."""
    data = kv_get(f"strategy_bundle::{bundle_name}", None) or {}
    trades = data.get("trades", [])
    trades.append(trade)
    data["trades"] = trades
    kv_set(f"strategy_bundle::{bundle_name}", data)
    return len(trades) - 1  # return index of new trade

def _close_trade_in_bundle(bundle_name, trade_index, updates):
    """Atomically close a trade in a strategy bundle in DB."""
    data = kv_get(f"strategy_bundle::{bundle_name}", None) or {}
    trades = data.get("trades", [])
    if 0 <= trade_index < len(trades):
        trades[trade_index].update(updates)
        data["trades"] = trades
        kv_set(f"strategy_bundle::{bundle_name}", data)

def _clear_stale_last_error():
    cfg = get_automation_config()
    cfg["last_error"] = None
    set_automation_config(cfg)

# ─── Calculation helpers ──────────────────────────────────────────────────────

# Real Zerodha F&O-Futures charges, from zerodha.com/charges (2026 rates). Every
# rate is here so it's trivial to update when Zerodha revises them — pass a
# {"charges": {...}} override in the futures config to change any one.
ZERODHA_FUT_CHARGES = {
    "brokerage_pct": 0.0003,     # 0.03% per executed order ...
    "brokerage_cap": 20.0,       # ... capped at Rs 20/leg (index futures hit the cap)
    "stt_sell":      0.0005,     # 0.05% on the SELL side (Apr 2026 revision)
    "txn_charge":    0.0000183,  # NSE 0.00183% of turnover, both sides
    "sebi_charge":   0.0000010,  # Rs 10 per crore of turnover
    "gst":           0.18,       # 18% on (brokerage + txn + SEBI)
    "stamp_buy":     0.00002,    # 0.002% on the BUY side
}


def _zerodha_futures_cost(buy_val, sell_val, cfg=None):
    """Round-trip Zerodha charges for one futures trade (a buy leg + a sell leg)."""
    r = dict(ZERODHA_FUT_CHARGES)
    if cfg and isinstance(cfg.get("charges"), dict):
        r.update(cfg["charges"])
    brokerage = min(r["brokerage_pct"] * buy_val, r["brokerage_cap"]) \
              + min(r["brokerage_pct"] * sell_val, r["brokerage_cap"])
    stt   = r["stt_sell"] * sell_val
    turn  = buy_val + sell_val
    txn   = r["txn_charge"] * turn
    sebi  = r["sebi_charge"] * turn
    gst   = r["gst"] * (brokerage + txn + sebi)
    stamp = r["stamp_buy"] * buy_val
    return brokerage + stt + txn + sebi + gst + stamp


def calc_futures_trades(trades, cfg):
    lot_size = int(cfg.get("lot_size") or 65)
    cap = float(cfg.get("capital") or 500_000)
    start = float(cfg.get("nifty_start") or 0)
    leverage = float(cfg.get("leverage") or 2)
    lots = max(int(round(leverage)), 1)
    qty = int(lot_size or 65) * int(lots or 2)
    running_cap = cap
    peak = cap
    results = []
    for t in trades:
        trend = t["trend"]
        entry = float(t.get("entry") or 0)
        # Single exit only — partial exits are no longer used. Use the first exit
        # price (falls back to entry so a still-open trade shows zero P&L).
        ex = t.get("partial_exit1")
        exit_px = float(ex) if ex not in ("", None) else entry
        q = qty
        # Direction sets which leg is the buy and which is the sell (STT is on the
        # sell side, stamp on the buy side). Costs = real Zerodha F&O futures charges.
        if trend == "LONG":
            buy_val, sell_val = entry * q, exit_px * q
            gross = (exit_px - entry) * q
        else:
            sell_val, buy_val = entry * q, exit_px * q
            gross = (entry - exit_px) * q
        cost = _zerodha_futures_cost(buy_val, sell_val, cfg)
        pnl = gross - cost
        running_cap += pnl
        peak = max(running_cap, peak)
        dd = 0 if running_cap >= peak else -1 * (1 - (100 + running_cap * 100) / (100 + 100 * peak))
        ret = pnl / cap
        net_pts = pnl / q if q else 0
        results.append({
            **t,
            "qty": q,
            "pnl": round(pnl, 2),
            "running_cap": round(running_cap, 2),
            "net_pts": round(net_pts, 2),
            "drawdown": round(dd * 100, 4),
            "trade_return": round(ret * 100, 4),
            "peak": round(peak, 2),
        })

    wins = [r for r in results if r["pnl"] >= 0]
    losses = [r for r in results if r["pnl"] < 0]
    curr_dd = results[-1]["drawdown"] if results else 0
    summary = {
        "curr_cap": round(running_cap, 2),
        "returns": round((running_cap / cap - 1) * 100, 2),
        "curr_dd": round(curr_dd, 4),
        "max_dd": round(min([r["drawdown"] for r in results], default=0), 4),
        "wins": len(wins),
        "losses": len(losses),
        "win_ratio": round(len(wins) / max(len(wins) + len(losses), 1) * 100, 1),
        "avg_gain": round(sum(r["pnl"] for r in wins) / max(len(wins), 1), 2),
        "avg_loss_amt": round(sum(r["pnl"] for r in losses) / max(len(losses), 1), 2),
        "biggest_win_ret": round(max([r["trade_return"] for r in wins], default=0), 4),
        "biggest_loss_ret": round(min([r["trade_return"] for r in losses], default=0), 4),
        "biggest_win": round(max([r["trade_return"] for r in wins], default=0), 4),
        "biggest_loss": round(min([r["trade_return"] for r in losses], default=0), 4),
        "avg_win": round(sum(r["trade_return"] for r in wins) / max(len(wins), 1), 4),
        "avg_loss_pct": round(sum(r["trade_return"] for r in losses) / max(len(losses), 1), 4),
        "avg_loss": round(sum(r["trade_return"] for r in losses) / max(len(losses), 1), 4),
        "total_points": round(sum(r["net_pts"] for r in results), 2),
        "min_capital_2lots": round((start * lot_size / leverage) * 2, 2),
    }
    return results, summary


def calc_spread_trades(trades, cfg, has_instrument=False):
    cap_start = float(cfg.get("capital") or 500_000)
    risk = float(cfg.get("risk_factor") or 0.10)
    lot_size = int(cfg.get("lot_size") or cfg.get("nifty_lot") or 65)
    finnifty_lot = int(cfg.get("finnifty_lot") or lot_size)
    strike_gap = int(cfg.get("strike_gap") or 100)
    tc = float(cfg.get("txn_cost") or 0.004)
    running_cap = cap_start
    peak = cap_start
    results = []
    for t in trades:
        trend = t["trend"]
        dd_prev = results[-1]["drawdown"] if results else 0
        instrument = t.get("instrument", "NIFTY") if has_instrument else "NIFTY"
        use_lot = finnifty_lot if instrument == "FINNIFTY" else lot_size
        if dd_prev > -10:
            qty = mround(running_cap * risk / strike_gap / use_lot, 1) * use_lot
        elif dd_prev > -20:
            qty = mround(running_cap * risk * 0.7 / strike_gap / use_lot, 1) * use_lot
        else:
            qty = mround(running_cap * risk * 0.5 / strike_gap / use_lot, 1) * use_lot
        qty = max(qty, use_lot)
        atm_s = float(t.get("atm_sell_price") or 0)
        atm_x = t.get("atm_exit_price") if t.get("atm_exit_price") not in ("", None) else atm_s
        otm_b = t["otm_buy_price"]
        otm_x = t.get("otm_exit_price") if t.get("otm_exit_price") not in ("", None) else otm_b
        pnl = ((otm_x - otm_b) + (atm_s - atm_x)) * qty - tc * qty * (atm_s + otm_b + atm_x + otm_x)
        running_cap += pnl
        peak = max(running_cap, peak)
        dd = 0 if running_cap >= peak else -1 * (1 - (100 + running_cap * 100) / (100 + 100 * peak))
        spot = t["spot"]
        atm_strike = mround(spot, 100)
        otm_strike = atm_strike - strike_gap if trend == "LONG" else atm_strike + strike_gap
        max_profit = qty * (atm_s - otm_b) / running_cap * 100
        max_loss = qty * (strike_gap - (atm_s - otm_b)) / running_cap * 100
        breakeven = atm_strike - (atm_s - otm_b) if trend == "LONG" else atm_strike + (atm_s - otm_b)
        capital_before_trade = running_cap - pnl
        return_on_cap = round(pnl / capital_before_trade * 100, 4) if capital_before_trade else 0
        r = {
            **t,
            "instrument": instrument,
            "qty": qty,
            "atm_strike": atm_strike,
            "otm_strike": otm_strike,
            "option_type": "PE" if trend == "LONG" else "CE",
            "pnl": round(pnl, 2),
            "running_cap": round(running_cap, 2),
            "return_on_cap": return_on_cap,
            "drawdown": round(dd * 100, 4),
            "peak": round(peak, 2),
            "max_profit_pct": round(max_profit, 4),
            "max_loss_pct": round(max_loss, 4),
            "breakeven": round(breakeven, 2),
        }
        if has_instrument:
            r["breakeven_spot"] = round(breakeven, 2)
        results.append(r)
    wins = [r for r in results if r["pnl"] >= 0]
    losses = [r for r in results if r["pnl"] < 0]
    curr_dd = results[-1]["drawdown"] if results else 0
    summary = {
        "curr_cap": round(running_cap, 2),
        "returns": round((running_cap / cap_start - 1) * 100, 2),
        "max_dd": round(min([r["drawdown"] for r in results], default=0), 4),
        "curr_dd": round(curr_dd, 4),
        "wins": len(wins),
        "losses": len(losses),
        "win_ratio": round(len(wins) / max(len(wins) + len(losses), 1) * 100, 1),
        "biggest_win": round(max([r["return_on_cap"] for r in wins], default=0), 4),
        "biggest_loss": round(min([r["return_on_cap"] for r in losses], default=0), 4),
        "avg_profit": round(sum(r["pnl"] for r in wins) / max(len(wins), 1) / cap_start * 100, 4),
        "avg_loss_amt": round(sum(r["pnl"] for r in losses) / max(len(losses), 1), 2),
        "avg_win": round(sum(r["return_on_cap"] for r in wins) / max(len(wins), 1), 4),
        "avg_loss": round(sum(r["return_on_cap"] for r in losses) / max(len(losses), 1), 4),
    }
    return results, summary

def calc_payoff(state):
    signal = state["signal"]
    atm_strike = mround(state["spot"], 100)
    otm_strike = atm_strike - state["strike_gap"] if signal == "LONG" else atm_strike + state["strike_gap"]
    atm_sell = state["atm_sell_premium"]
    otm_buy = state["otm_buy_premium"]
    qty = state["quantity"]
    cap = state["capital"]
    rows = []
    start_spot = (otm_strike - 200) if signal == "LONG" else (atm_strike - 200)
    for i in range(21):
        sp = start_spot + i * 25
        if signal == "LONG":
            atm_val = 0 if sp >= atm_strike else atm_strike - sp
            otm_val = 0 if sp >= otm_strike else otm_strike - sp
        else:
            atm_val = 0 if sp <= atm_strike else sp - atm_strike
            otm_val = 0 if sp <= otm_strike else sp - otm_strike
        net_atm = atm_sell - atm_val
        net_otm = otm_val - otm_buy
        net_pts = net_atm + net_otm
        pl = qty * net_pts
        rows.append({"spot": sp, "atm_payoff": round(atm_val, 2), "otm_payoff": round(otm_val, 2),
                     "net_pts": round(net_pts, 2), "pl": round(pl, 2)})
    breakeven = atm_strike - (atm_sell - otm_buy) if signal == "LONG" else atm_strike + (atm_sell - otm_buy)
    max_pl = max(r["pl"] for r in rows)
    min_pl = min(r["pl"] for r in rows)
    return rows, {
        "atm_strike": atm_strike, "otm_strike": otm_strike,
        "breakeven": round(breakeven, 2),
        "max_profit": round(max_pl, 2), "max_profit_pct": round(max_pl / cap * 100, 2),
        "max_loss": round(min_pl, 2), "max_loss_pct": round(min_pl / cap * 100, 2),
        "risk_reward": round(abs(min_pl) / max(max_pl, 1), 2),
    }

# ─── Routes ──────────────────────────────────────────────────────────────────



@app.route("/api/master", methods=["GET", "POST"])
def api_master():
    global master_state
    if request.method == "POST":
        data = request.json or {}
        persist_master_state_updates(data)
    refresh_master_state_from_db()
    master_state["nifty_lot_size"] = CURRENT_NIFTY_LOT_SIZE
    spot = master_state["nifty_spot"]
    trade_level = master_state["nifty_trade_level"]
    lot = master_state["nifty_lot_size"]
    notional = lot * spot
    margin = mround(notional, 1) * 0.2
    cost_buy = notional * 0.00007
    cost_sell = notional * 0.00015
    pct_change = spot / trade_level - 1
    payload = {
        **master_state,
        "pct_change": round(pct_change * 100, 2),
        "notional_per_lot": round(notional, 2),
        "margin_per_lot": round(margin, 2),
        "cost_buy_per_lot": round(cost_buy, 2),
        "cost_sell_per_lot": round(cost_sell, 2),
        "today": date.today().isoformat(),
        "current_year": date.today().year,
        "nifty_lot_size": CURRENT_NIFTY_LOT_SIZE,
        "signal_time_ist": _to_ist_display(master_state.get("signal_time")),
    }
    return jsonify(payload)

@app.route("/api/futures", methods=["GET", "POST"])
def api_futures():
    bundle = kv_get(read_bundle_key("futures"), {}) or {}
    cfg = bundle.get("config") or futures_config
    trades = bundle.get("trades") or []
    if request.method == "POST":
        body = request.json or {}
        if "config" in body:
            cfg.update(body["config"])
            futures_config.update(body["config"])
        # trades are managed by automation sync only - frontend cannot overwrite
    results, summary = calc_futures_trades(trades, cfg)
    archive = paper_phase_archive(bundle)
    arch_results, arch_summary = calc_futures_trades(archive, cfg) if archive else ([], {})
    return jsonify({"config": cfg, "trades": results, "summary": summary,
                    "archive": arch_results, "archive_summary": arch_summary})




@app.route("/api/nexp_workstation", methods=["GET", "POST"])
def api_nexp_workstation():
    bundle = kv_get(read_bundle_key("nexp_workstation"), {}) or {}
    cfg = bundle.get("config") or nexp_workstation_config
    trades = bundle.get("trades") or []
    if request.method == "POST":
        body = request.json or {}
        if "config" in body:
            cfg.update(body["config"])
            nexp_workstation_config.update(body["config"])
        # trades are managed by automation sync only - frontend cannot overwrite
    results, summary = calc_spread_trades(trades, cfg)
    return jsonify({"config": cfg, "trades": results, "summary": summary,
                    "archive": paper_phase_archive(bundle)})


def calc_strangle_trades(trades, cfg):
    """P&L summary for the Nifty Strangle 3.5% model — two independent naked-
    sell legs per trade (not a defined-risk spread), so this mirrors
    calc_spread_trades' running-capital/drawdown bookkeeping but sums P&L
    across however many legs (and re-entries) a cycle produced."""
    cap_start = float(cfg.get("capital") or 1_000_000)
    tc = float(cfg.get("txn_cost") or 0.004)
    running_cap = cap_start
    peak = cap_start
    results = []
    for t in trades:
        legs = t.get("legs") or {}
        pnl = 0.0
        all_closed = True
        # Keyed dict (ce/pe -> leg), NOT a list — the frontend reads
        # t.legs.ce / t.legs.pe directly. This used to build a plain list via
        # .append(), which silently discarded the ce/pe keys: the API response
        # still LOOKED right (pnl/stats compute fine either way, since those
        # are summed before this dict/list choice), but every leg-level cell
        # (Strike/Entry/Exit/Qty) rendered blank/zero because legs.ce and
        # legs.pe were undefined on a list. Dormant until the first trade ever
        # actually populated a row (the historical backfill).
        leg_rows = {}
        total_qty = 0
        total_entry_premium = 0.0
        for key, leg in legs.items():
            entry = float(leg.get("entry_price") or 0)
            exitp = leg.get("exit_price")
            qty = int(leg.get("qty") or 0)
            total_qty = max(total_qty, qty)
            total_entry_premium += entry * qty
            if leg.get("status") != "CLOSED" or exitp in (None, ""):
                all_closed = False
                leg_rows[key] = {**leg, "pnl": None}
                continue
            exitp = float(exitp)
            leg_pnl = (entry - exitp) * qty - tc * qty * (entry + exitp)
            pnl += leg_pnl
            leg_rows[key] = {**leg, "pnl": round(leg_pnl, 2)}
        capital_before = running_cap
        if not all_closed:
            # still-open trade: show unrealised so far from closed legs only
            running_after = running_cap + pnl
        else:
            running_cap += pnl
            running_after = running_cap
        is_new_peak = running_after >= peak and pnl >= 0
        peak = max(running_after, peak)
        dd = 0 if running_after >= peak else -1 * (1 - (100 + running_after * 100) / (100 + 100 * peak))
        return_on_cap = round(pnl / capital_before * 100, 4) if capital_before else 0
        # Max profit (excl charges): total premium collected across both legs, if
        # both expired worthless — the ceiling outcome for a short strangle.
        # Max loss has NO defined ceiling here (unlike the AIT/NiftyEXP credit
        # spread, which has a hedge): a naked strangle's loss is theoretically
        # unbounded if spot runs past either short strike, so it is intentionally
        # NOT shown as a percentage — see "Unlimited" in the frontend column.
        max_profit_pct = round(total_entry_premium / capital_before * 100, 4) if capital_before else 0
        r = {
            **t,
            "legs": leg_rows,
            "pnl": round(pnl, 2),
            "running_cap": round(running_after, 2),
            "return_on_cap": return_on_cap,
            "drawdown": round(dd * 100, 4),
            "peak": round(peak, 2),
            "max_profit_pct": max_profit_pct,
            "is_new_peak": bool(is_new_peak),
        }
        results.append(r)

    won = sum(1 for r in results if r.get("pnl", 0) > 0 and r.get("status") == "CLOSED")
    lost = sum(1 for r in results if r.get("pnl", 0) <= 0 and r.get("status") == "CLOSED")
    closed_results = [r for r in results if r.get("status") == "CLOSED"]
    summary = {
        "current_capital": round(running_cap, 2),
        "returns_pct": round((running_cap - cap_start) / cap_start * 100, 2) if cap_start else 0,
        "max_drawdown": round(min([r["drawdown"] for r in results], default=0), 4),
        "trades_won": won,
        "trades_lost": lost,
        "win_ratio": round(won / (won + lost) * 100, 2) if (won + lost) else 0,
        "biggest_win": round(max([r["pnl"] for r in closed_results], default=0), 2),
        "biggest_loss": round(min([r["pnl"] for r in closed_results], default=0), 2),
    }
    return results, summary


@app.route("/api/nifty_strangle_w", methods=["GET", "POST"])
def api_nifty_strangle_w():
    """Nifty Strangle 3.5% — naked SELL CE (spot+3.5%) + SELL PE (spot-3.5%),
    weekly expiry. Entry Wed 09:20 AM, hard square-off 15:38 PM, plus per-leg
    Target/SL/Trailing-SL/Re-entry managed continuously by monitor_nifty_strangle_w_legs
    (models_v2.py). Starts in Paper mode (see DEFAULT_MODEL_MODES) — flip it on
    the go-live board once you're happy with paper results."""
    bundle = kv_get(read_bundle_key("nifty_strangle_w"), {}) or {}
    default_cfg = {k: v for k, v in models_v2.DEFAULT_NIFTY_STRANGLE_W_CONFIG.items() if k != "legs"}
    default_cfg["legs"] = {k: dict(v) for k, v in models_v2.DEFAULT_NIFTY_STRANGLE_W_CONFIG["legs"].items()}
    cfg = bundle.get("config") or default_cfg
    trades = bundle.get("trades") or []
    if request.method == "POST":
        body = request.json or {}
        if "config" in body:
            incoming = body["config"] or {}
            if "legs" in incoming and isinstance(incoming["legs"], dict):
                cfg.setdefault("legs", default_cfg["legs"])
                for leg_key, leg_val in incoming["legs"].items():
                    if isinstance(leg_val, dict):
                        cfg["legs"].setdefault(leg_key, {})
                        cfg["legs"][leg_key].update(leg_val)
            for k, v in incoming.items():
                if k != "legs":
                    cfg[k] = v
            # Only persist — and only touch "config" — if it actually changed.
            # The frontend POSTs its config on every ~5s poll, not just on real
            # edits; writing unconditionally hammered kv_set with the *stale*
            # trades list captured above, which trips its "clearing trades"
            # warning on every poll (harmless here — this model just hasn't
            # traded yet — but noisy) and, worse, could race a scheduler write
            # that lands between our read and write. Re-reading right before
            # the write and only ever setting "config" (never "trades") avoids
            # both: trades stay exclusively owned by the scheduler.
            if cfg != (bundle.get("config") or {}):
                fresh = kv_get(read_bundle_key("nifty_strangle_w"), {}) or {}
                fresh["config"] = cfg
                kv_set(read_bundle_key("nifty_strangle_w"), fresh)
                bundle = fresh
                trades = fresh.get("trades") or []
        # trades are managed by automation sync only — frontend cannot overwrite
    results, summary = calc_strangle_trades(trades, cfg)
    return jsonify({"config": cfg, "trades": results, "summary": summary,
                    "archive": paper_phase_archive(bundle)})


NIFTY_50_INDEX_TOKEN = 256265  # NSE:NIFTY 50, standard Zerodha instrument token


def backfill_nifty_strangle_row(target_date_str, user_id=None):
    """Reconstruct ONE real historical Nifty Strangle 3.5% trade from ACTUAL
    Kite historical spot + option premium data (not a simulation): entry Wed
    09:20 AM, exit the FOLLOWING Tuesday ~15:38 (6-day hold, matching the
    corrected 2026-08-07 spec — this is NOT a same-day close). Writes into
    that user's own trade log; places no order. Returns (ok, message)."""
    try:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except Exception:
        return False, "Invalid date — expected YYYY-MM-DD"
    if target_date.weekday() != 2:
        return False, f"{target_date_str} is not a Wednesday"

    exit_date = target_date + timedelta(days=1)
    while exit_date.weekday() != 1:  # 1 = Tuesday, weekly NIFTY expiry
        exit_date += timedelta(days=1)
    if exit_date >= date.today():
        return False, (f"Can't backfill yet — this cycle's exit ({exit_date.isoformat()}) "
                        f"hasn't happened yet. Pick an earlier Wednesday.")
    if get_model_mode("nifty_strangle_w", user_id) == "live":
        return False, ("Model is LIVE for this account — backfill is only allowed in "
                        "Paper mode, to avoid writing a fabricated row into a live record")

    try:
        kite = get_kite(require_token=True, user_id=user_id)
    except Exception as e:
        return False, f"Zerodha not connected: {e}"

    def _dtstr(day, hh, mm):
        d = datetime(day.year, day.month, day.day, hh, mm, tzinfo=APP_TZ)
        return d.strftime("%Y-%m-%d %H:%M:%S")

    def _nearest(candles, hh, mm):
        for c in candles:
            ts = c["date"]
            if (ts.hour, ts.minute) >= (hh, mm):
                return c
        return candles[-1] if candles else None

    try:
        spot_candles = kite.historical_data(NIFTY_50_INDEX_TOKEN, _dtstr(target_date, 9, 15), _dtstr(target_date, 9, 40), "minute")
    except Exception as e:
        return False, f"NIFTY 50 historical data fetch failed: {e}"
    if not spot_candles:
        return False, "No NIFTY 50 historical candles for that morning (market closed, or date out of Kite's history range)"

    entry_candle = _nearest(spot_candles, 9, 20)
    spot = float(entry_candle["close"])
    ce_strike = models_v2._pct_otm_strike(spot, 3.5, "+")
    pe_strike = models_v2._pct_otm_strike(spot, 3.5, "-")

    expiry = exit_date  # weekly contract's expiry IS the Tuesday it's exited on

    try:
        nfo = kite.instruments("NFO")
    except Exception as e:
        return False, f"NFO instrument fetch failed: {e}"

    def find(strike, opt_type):
        for row in nfo:
            if (row.get("name") == "NIFTY" and row.get("strike") == strike
                    and row.get("instrument_type") == opt_type and row.get("expiry") == expiry):
                return row
        return None

    ce_inst = find(ce_strike, "CE")
    pe_inst = find(pe_strike, "PE")
    if not ce_inst or not pe_inst:
        return False, f"Contract lookup failed (CE found={bool(ce_inst)}, PE found={bool(pe_inst)}) for expiry {expiry.isoformat()}"

    def premium_at(token, day, hh, mm):
        try:
            candles = kite.historical_data(token, _dtstr(day, hh, mm), _dtstr(day, hh, min(mm + 5, 59)), "minute")
        except Exception:
            return None
        c = _nearest(candles, hh, mm) if candles else None
        return float(c["close"]) if c else None

    ce_entry = premium_at(ce_inst["instrument_token"], target_date, 9, 20)
    pe_entry = premium_at(pe_inst["instrument_token"], target_date, 9, 20)
    ce_exit  = premium_at(ce_inst["instrument_token"], exit_date, 15, 38)
    pe_exit  = premium_at(pe_inst["instrument_token"], exit_date, 15, 38)
    if None in (ce_entry, pe_entry, ce_exit, pe_exit):
        return False, "Missing historical premium data for one or both legs at entry/exit time"

    qty = 10 * models_v2.NIFTY_LOT_SIZE
    default_ce_cfg = models_v2._default_strangle_leg("+")
    default_pe_cfg = models_v2._default_strangle_leg("-")

    new_trade = {
        "date": target_date.isoformat(), "spot": spot, "expiry": expiry.isoformat(),
        "status": "CLOSED", "exit_date": exit_date.isoformat(),
        "legs": {
            "ce": {"tradingsymbol": ce_inst["tradingsymbol"], "strike": ce_strike, "option_type": "CE",
                   "entry_price": ce_entry, "exit_price": ce_exit, "qty": qty, "status": "CLOSED",
                   "exit_reason": "historical_backfill", "reentry_count": 0,
                   "trail_armed": False, "trail_stop": None, "config": default_ce_cfg},
            "pe": {"tradingsymbol": pe_inst["tradingsymbol"], "strike": pe_strike, "option_type": "PE",
                   "entry_price": pe_entry, "exit_price": pe_exit, "qty": qty, "status": "CLOSED",
                   "exit_reason": "historical_backfill", "reentry_count": 0,
                   "trail_armed": False, "trail_stop": None, "config": default_pe_cfg},
        },
    }

    bundle_key = _bundle_key_for("nifty_strangle_w", user_id)
    data = kv_get(bundle_key, {}) or {}
    trades = data.get("trades", [])
    # Replace (not skip) any existing row for this entry date — a re-fetch is
    # expected to supersede a previous backfill for the same Wednesday (e.g.
    # after the same-day-close -> 6-day-hold correction on 2026-08-07).
    trades = [t for t in trades if not (isinstance(t, dict) and t.get("date") == new_trade["date"]
                                         and t.get("status") == "CLOSED"
                                         and (t.get("legs") or {}).get("ce", {}).get("exit_reason") == "historical_backfill")]

    trades.insert(0, new_trade)
    data["trades"] = trades
    kv_set(bundle_key, data)
    log_automation(f"Nifty Strangle 3.5%: backfilled historical row {target_date.isoformat()} -> {exit_date.isoformat()} "
                    f"(user_id={user_id}, spot={spot}, CE {ce_strike}, PE {pe_strike})", level="INFO")
    return True, (f"Backfilled {target_date.isoformat()} → exit {exit_date.isoformat()} — "
                   f"spot {spot:.2f}, CE {ce_strike} ({ce_entry}→{ce_exit}), PE {pe_strike} ({pe_entry}→{pe_exit})")


@app.route("/api/nifty_strangle_w/backfill", methods=["POST"])
def api_nifty_strangle_w_backfill():
    """Manual 'fetch historical data' button on the Nifty Strangle page —
    reconstructs one real past Wednesday's trade from actual Kite history so
    you can see how the (now-fixed) strategy would have performed, without
    waiting for the next live Wednesday. Places no order."""
    if "user_id" not in session and multi_tenant_enabled():
        return jsonify({"ok": False, "error": "login required"}), 401
    body = request.json or {}
    target_date_str = body.get("date")
    if not target_date_str:
        return jsonify({"ok": False, "error": "date is required (YYYY-MM-DD)"}), 400
    uid = session.get("user_id") if multi_tenant_enabled() else None
    ok, message = backfill_nifty_strangle_row(target_date_str, user_id=uid)
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


@app.route("/api/finnifty_exp", methods=["GET", "POST"])
def api_finnifty_exp():
    global finnifty_exp_trades, finnifty_exp_config
    if request.method == "POST":
        body = request.json
        if "config" in body:
            finnifty_exp_config.update(body["config"])
        if "trades" in body:
            finnifty_exp_trades = body["trades"]
    results, summary = calc_spread_trades(finnifty_exp_trades, finnifty_exp_config)
    return jsonify({"config": finnifty_exp_config, "trades": results, "summary": summary})

@app.route("/api/stairs_lr", methods=["GET", "POST"])
def api_stairs_lr():
    global stairs_lr_trades, stairs_lr_config
    if request.method == "POST":
        body = request.json
        if "config" in body:
            stairs_lr_config.update(body["config"])
        if "trades" in body:
            stairs_lr_trades = body["trades"]
    results, summary = calc_spread_trades(stairs_lr_trades, stairs_lr_config, has_instrument=True)
    return jsonify({"config": stairs_lr_config, "trades": results, "summary": summary})

@app.route("/api/stairs_hr", methods=["GET", "POST"])
def api_stairs_hr():
    global stairs_hr_trades, stairs_hr_config
    if request.method == "POST":
        body = request.json
        if "config" in body:
            stairs_hr_config.update(body["config"])
        if "trades" in body:
            stairs_hr_trades = body["trades"]
    results, summary = calc_spread_trades(stairs_hr_trades, stairs_hr_config, has_instrument=True)
    return jsonify({"config": stairs_hr_config, "trades": results, "summary": summary})

@app.route("/api/payoff", methods=["GET", "POST"])
def api_payoff():
    global payoff_state
    if request.method == "POST":
        payoff_state.update(request.json)
        payoff_state["atm_sell_strike"] = mround(payoff_state["spot"], 100)
        if payoff_state["signal"] == "LONG":
            payoff_state["otm_buy_strike"] = payoff_state["atm_sell_strike"] - payoff_state["strike_gap"]
        else:
            payoff_state["otm_buy_strike"] = payoff_state["atm_sell_strike"] + payoff_state["strike_gap"]
    rows, meta = calc_payoff(payoff_state)
    return jsonify({"state": payoff_state, "rows": rows, "meta": meta})



@app.route("/api/zerodha/nifty_futures_quote")
def api_zerodha_nifty_futures_quote():
    try:
        contract = pick_nifty_futures_contract()
        instrument_key = f"{contract['exchange']}:{contract['tradingsymbol']}"
        kite = get_kite(require_token=True)
        ltp_data = kite.ltp([instrument_key])
        premium = float(ltp_data[instrument_key]["last_price"])
        return jsonify({
            "ok": True,
            "cmp": premium,
            "lot_size": contract["lot_size"],
            "expiry": contract["expiry"],
            "tradingsymbol": contract["tradingsymbol"],
            "entry_date": date.today().isoformat(),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/zerodha/status")
def api_zerodha_status():
    # ?user_id=<id> -> that user's own connection. Otherwise the logged-in user's
    # own token, falling back to the instance/global token.
    uid = request.args.get("user_id") or session.get("user_id")
    per_user_token = get_access_token(uid) if uid else None
    connected = bool(per_user_token or get_access_token() or session.get("kite_access_token"))
    return jsonify({
        "configured": zerodha_ready(uid),
        "has_creds": bool(get_user_kite_creds(uid)) if uid else False,
        "connected": connected,
        "user_connected": bool(per_user_token),
        "token_fresh": is_token_fresh(),
        "token_set_at": get_token_set_at(uid) if per_user_token else kv_get('kite_token_set_at', None),
        "has_kiteconnect": KiteConnect is not None,
        "user_id": kv_get("kite_zuser::%s" % uid, None) if per_user_token else kv_get("kite_user_id", None),
    })

@app.route("/api/zerodha/login_url")
def api_zerodha_login_url():
    try:
        uid = session.get("user_id")
        kite = get_kite(require_token=False, user_id=uid)   # build with THIS user's api_key
        return jsonify({"ok": True, "login_url": kite.login_url()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/zerodha/credentials", methods=["GET", "POST"])
def api_zerodha_credentials():
    """Each user stores their OWN Kite Connect app credentials (api key + secret).
    Zerodha ties one app to one account, so multi-user requires per-user apps."""
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    uid = session["user_id"]
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        api_key = (body.get("api_key") or "").strip()
        api_secret = (body.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            return jsonify({"ok": False, "error": "Both API key and API secret are required."}), 400
        kv_set("kite_creds::%s" % uid, {"api_key": api_key, "api_secret": api_secret})
        log_automation("Zerodha API credentials saved", details={"app_user": uid})
        return jsonify({"ok": True})
    c = get_user_kite_creds(uid)
    return jsonify({"ok": True, "has_creds": bool(c),
                    "api_key_masked": (c[0][:4] + "****" + c[0][-2:]) if c else None})


@app.route("/api/telegram/chat_id", methods=["GET", "POST"])
def api_telegram_chat_id():
    """Each user stores their own Telegram chat id so alerts reach THEIR Telegram."""
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    uid = session["user_id"]
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        cid = str(body.get("chat_id", "")).strip()
        conn = get_db()
        conn.execute("UPDATE users SET telegram_chat_id=? WHERE id=?", (cid or None, uid))
        conn.commit(); conn.close()
        log_automation("Telegram chat id updated", details={"app_user": uid})
        return jsonify({"ok": True})
    return jsonify({"ok": True, "chat_id": get_user_telegram_chat(uid) or "",
                    "bot_configured": bool(TELEGRAM_BOT_TOKEN)})


@app.route("/api/telegram/test", methods=["POST"])
def api_telegram_test():
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "login required"}), 401
    uid = session["user_id"]
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"ok": False, "error": "Telegram bot is not configured on the server."}), 400
    cid = get_user_telegram_chat(uid)
    if not cid:
        return jsonify({"ok": False, "error": "Save your Telegram Chat ID first."}), 400
    u = get_user_by_id(uid)
    name = (u.get("name") or "").split(" ")[0] if u else ""
    msg = (f"✅ <b>STAIRS test alert</b>\nHi {name} — your Telegram is linked. "
           f"You'll receive your model &amp; order alerts here.")
    try:
        resp = _requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": msg, "parse_mode": "HTML"}, timeout=8)
        j = resp.json()
        if j.get("ok"):
            return jsonify({"ok": True})
        desc = j.get("description", "Telegram rejected the message.")
        # common case: user hasn't started the bot yet
        if "chat not found" in desc.lower() or "can't initiate" in desc.lower() or "blocked" in desc.lower():
            desc += "  → Open the STAIRS bot in Telegram and tap Start, then try again."
        return jsonify({"ok": False, "error": desc}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/zerodha/callback")
def api_zerodha_callback():
    # Shared Kite Redirect URL. STAIRS (React) sets stairs_zerodha_oauth=1 before
    # opening Kite login; forward the unused request_token to FastAPI so STAIRS
    # can exchange it and return to /stairs/. Without the cookie → this Flask app.
    if request.cookies.get("stairs_zerodha_oauth") == "1":
        target = "/api/broker/zerodha/callback"
        if request.query_string:
            target = f"{target}?{request.query_string.decode('utf-8', errors='ignore')}"
        app.logger.info("Zerodha callback handoff → STAIRS FastAPI (%s)", target)
        resp = redirect(target)
        resp.set_cookie(
            "stairs_zerodha_oauth",
            "",
            max_age=0,
            path="/",
            secure=True,
            samesite="Lax",
        )
        return resp

    request_token = request.args.get("request_token")
    status = request.args.get("status")
    masked_api_key = f"{KITE_API_KEY[:4]}****" if KITE_API_KEY else "missing"
    app.logger.info("Zerodha callback received", extra={})
    app.logger.info("status=%s request_token_present=%s api_key=%s api_secret_loaded=%s remote_addr=%s",
                    status, bool(request_token), masked_api_key, bool(KITE_API_SECRET), request.remote_addr)
    try:
        if not request_token:
            return "Missing request_token", 400
        _app_uid = session.get("user_id")
        api_key, api_secret = resolve_kite_creds(_app_uid)
        if not api_key or not api_secret:
            return "No Kite API credentials found for your account. Add your API key/secret first.", 400
        kite = KiteConnect(api_key=api_key)              # THIS user's own app
        data = kite.generate_session(request_token, api_secret=api_secret)
        session["kite_access_token"] = data["access_token"]
        session["kite_user_id"] = data.get("user_id")
        if _app_uid:
            # Store the token under the logged-in app user (their own Zerodha).
            set_access_token(data["access_token"], user_id=_app_uid)
            kv_set("kite_zuser::%s" % _app_uid, data.get("user_id"))
            # Only the admin/instance owns the shared GLOBAL token — a regular
            # user's connect must never clobber it (would cross accounts).
            _u = get_user_by_id(_app_uid)
            if _u and _u.get("is_admin"):
                set_access_token(data["access_token"])
                kv_set("kite_user_id", data.get("user_id"))
        else:
            set_access_token(data["access_token"])
            kv_set("kite_user_id", data.get("user_id"))
        log_automation("Zerodha session connected", details={"user_id": data.get("user_id"), "app_user": _app_uid})
        _clear_stale_last_error()
        app.logger.info("Zerodha login successful for user_id=%s", data.get("user_id"))
        return redirect("/dashboard")
    except Exception as e:
        app.logger.exception("Zerodha login failed")
        return f"Zerodha login failed: {e}", 400

@app.route("/api/zerodha/option_quote", methods=["POST"])
def api_zerodha_option_quote():
    try:
        body = request.get_json(force=True)
        signal = body.get("signal")
        spot = float(body.get("spot"))
        expiry = body.get("expiry")
        capital = float(body.get("capital"))
        risk_pct = float(body.get("risk_pct"))
        lot_size = int(body.get("lot_size"))
        if not expiry:
            return jsonify({"ok": False, "error": "Expiry date is required. Please wait for page to load fully."}), 400
        # Options Buy panel is directional: LONG->CE, SHORT->PE
        _opt_type = "CE" if str(signal).upper() == "LONG" else "PE"
        contract = pick_nifty_option_contract_by_type(_opt_type, spot, expiry)
        instrument_key = f"{contract['exchange']}:{contract['tradingsymbol']}"
        kite = get_kite(require_token=True)
        ltp_data = kite.ltp([instrument_key])
        premium = float(ltp_data[instrument_key]["last_price"])
        risk_budget = capital * risk_pct
        cost_per_lot = premium * lot_size
        lots = math.floor(risk_budget / cost_per_lot) if cost_per_lot > 0 else 0
        quantity = lots * lot_size
        return jsonify({
            "ok": True,
            "signal": signal,
            "spot": spot,
            "atm_strike": contract["strike"],
            "option_type": contract["option_type"],
            "expiry": contract["expiry"],
            "tradingsymbol": contract["tradingsymbol"],
            "premium": premium,
            "risk_budget": round(risk_budget, 2),
            "cost_per_lot": round(cost_per_lot, 2),
            "lots": lots,
            "quantity": quantity,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400



@app.route("/api/zerodha/spread_quote", methods=["POST"])
def api_zerodha_spread_quote():
    try:
        body = request.get_json(force=True)
        signal = body.get("signal")
        spot = float(body.get("spot"))
        expiry = body.get("expiry") or None
        strike_gap = int(body.get("strike_gap") or 100)
        data = get_nifty_spread_quote(signal, spot, expiry, strike_gap)
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/zerodha/instruments_status")
def api_zerodha_instruments_status():
    return jsonify({
        "loaded_at": instrument_cache.get("loaded_at").isoformat() if hasattr(instrument_cache.get("loaded_at"), "isoformat") else instrument_cache.get("loaded_at"),
        "refreshed_at": instrument_cache.get("refreshed_at"),
        "rows": len(instrument_cache.get("rows") or []),
    })

@app.route("/api/automation/status")
def api_automation_status():
    refresh_master_state_from_db()
    cfg = get_automation_config()
    last_error = cfg.get("last_error")
    if last_error and "Incorrect `api_key` or `access_token`" in str(last_error) and get_access_token():
        last_error = None
    return jsonify({
        "kill_switch": bool(automation_state.get("kill_switch")),
        "automation_enabled": bool(automation_state.get("automation_enabled")),
        "mode": cfg.get("mode", "DRY_RUN"),
        "last_run_at": cfg.get("last_run_at"),
        "last_action": cfg.get("last_action"),
        "last_error": last_error,
        "active_position": get_active_position(),
    })




@app.route("/api/automation/logs")
def api_automation_logs():
    # Admin-only: this feed includes internal stack traces / file paths from
    # kv_set's diagnostic warnings and isn't meant for a general audience.
    if "user_id" not in session:
        return jsonify({"error": "login required"}), 401
    _u = get_user_by_id(session["user_id"])
    if not _u or not _u.get("is_admin"):
        return jsonify({"error": "Admin access required"}), 403
    limit = int(request.args.get('limit', '50'))
    rows = get_automation_logs(limit)
    for row in rows:
        row["created_at_ist"] = _to_ist_display(row.get("created_at"))
    return jsonify({"logs": rows})

def _require_admin_json():
    """Inline admin gate for API routes defined before admin_required exists
    in the file. Returns a (jsonify, status) tuple to return early, or None
    if the caller is a logged-in admin."""
    if "user_id" not in session:
        return jsonify({"error": "login required"}), 401
    _u = get_user_by_id(session["user_id"])
    if not _u or not _u.get("is_admin"):
        return jsonify({"error": "Admin access required"}), 403
    return None

@app.route("/api/automation/kill", methods=["POST"])
def api_automation_kill():
    # Halts LIVE order placement for every model, every user — admin-only.
    # (Previously unauthenticated: on 2026-07-22 this was hit directly,
    # unattributed, three times in 35 seconds — no UI ever called it.)
    _deny = _require_admin_json()
    if _deny:
        return _deny
    automation_state["kill_switch"] = True
    automation_state["automation_enabled"] = False
    save_automation_state()
    cfg = get_automation_config(); cfg['last_action'] = 'Kill switch activated'; set_automation_config(cfg)
    log_automation(f'Kill switch activated by {session.get("user_id")}', level='WARNING')
    return api_automation_status()

@app.route("/api/automation/reset_kill", methods=["POST"])
def api_automation_reset_kill():
    _deny = _require_admin_json()
    if _deny:
        return _deny
    automation_state["kill_switch"] = False
    save_automation_state()
    cfg = get_automation_config(); cfg['last_action'] = 'Kill switch reset'; set_automation_config(cfg)
    log_automation(f'Kill switch reset by {session.get("user_id")}')
    return api_automation_status()

@app.route("/api/automation/mode", methods=["POST"])
def api_automation_mode():
    """RETIRED 2026-07-24 — the global DRY_RUN/LIVE switch was removed. Live vs
    paper is now controlled per-model via /api/automation/live_models (go-live board)."""
    return jsonify({'ok': False, 'retired': True,
                    'error': 'Global mode retired; use per-model live_models'}), 410


@app.route("/api/automation/live_models", methods=["GET", "POST"])
def api_live_models():
    """Per-model live-order enablement. A model places REAL orders only if the
    global mode is LIVE AND it is listed here. This is the go-live switch for
    each model (default: futures only)."""
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        requested = body.get("models", [])
        if not isinstance(requested, list):
            return jsonify({"ok": False, "error": "models must be a list"}), 400
        invalid = [m for m in requested if m not in KNOWN_LIVE_MODELS]
        if invalid:
            return jsonify({"ok": False, "error": f"unknown models: {invalid}",
                            "known": list(KNOWN_LIVE_MODELS)}), 400
        # Archive paper trades for any model going Paper -> Live, so live trading
        # starts fresh from the ORIGINAL configured capital (paper gains ignored).
        prev_live = set(get_live_enabled_models())
        archived = {}
        for m in requested:
            if m not in prev_live:
                n = archive_and_reset_model(m, phase="paper", reason="go_live")
                if n:
                    archived[m] = n
        saved = set_live_enabled_models(requested)
        log_automation(f"live_enabled_models set to {saved} (archived paper: {archived})", level="INFO")
        try:
            send_telegram(f"⚙️ <b>Live models updated</b>\nLive-enabled: {', '.join(saved) or '(none)'}")
        except Exception:
            pass
        return jsonify({"ok": True, "live_enabled_models": saved, "known": list(KNOWN_LIVE_MODELS)})
    # Multi-tenant: report the LOGGED-IN user's live models so the dashboard routes
    # each model's trades to the right (live vs paper) row.
    _luid = session.get("user_id") if multi_tenant_enabled() else None
    return jsonify({"ok": True, "live_enabled_models": get_live_enabled_models(_luid),
                    "known": list(KNOWN_LIVE_MODELS),
                    "meta": LIVE_MODEL_META,
                    "mode": get_automation_config().get("mode", "DRY_RUN")})


@app.route("/api/model/clear_seed", methods=["POST"])
def api_clear_seed():
    """Clear a model's current trades (old seed/backtest data). Non-destructive:
    the cleared trades are kept in `seed_backup` so they can be restored."""
    body = request.get_json(force=True) or {}
    model = body.get("model")
    key = _bundle_key(model)
    if not key:
        return jsonify({"ok": False, "error": "unknown model", "known": list(KNOWN_LIVE_MODELS)}), 400
    data = kv_get(key, {}) or {}
    old = data.get("trades", []) or []
    data["seed_backup"] = old
    data["trades"] = []
    kv_set(key, data)
    log_automation(f"{model}: cleared {len(old)} seed/old trade(s) (backed up to seed_backup)", level="INFO")
    return jsonify({"ok": True, "model": model, "cleared": len(old)})


def _open_positions_by_model(user_id=None):
    """Count OPEN trades per model (used by the deactivation modal). Per-user if given."""
    out = {}
    for m in KNOWN_LIVE_MODELS:
        key = read_bundle_key(m, user_id) if user_id else ("strategy_bundle::" + m)
        data = kv_get(key, {}) or {}
        trades = data.get("trades", []) or []
        out[m] = sum(1 for t in trades if isinstance(t, dict)
                     and str(t.get("status", "")).upper() == "OPEN")
    return out


@app.route("/api/automation/model_modes", methods=["GET", "POST"])
def api_model_modes():
    """Per-model execution mode: off / paper / live.
    ?user_id=<id> scopes to that user's modes (multi-tenant, admin-only to edit);
    omitted = the instance/global modes the current single-account engine uses."""
    uid = request.args.get("user_id")
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        uid = uid or body.get("user_id")
        # Role-based control. You must be logged in. You may edit only YOUR OWN
        # modes unless you're admin (admins manage any user). And you may switch
        # only the models your tier grants access to (Standard->Futures,
        # Premium->all, Basic->none); admins control everything.
        acting = get_user_by_id(session["user_id"]) if session.get("user_id") else None
        if not acting:
            return jsonify({"ok": False, "error": "login required"}), 401
        is_admin = bool(acting.get("is_admin"))
        if uid and str(uid) != str(acting.get("id")) and not is_admin:
            return jsonify({"ok": False, "error": "admin required to edit another user's modes"}), 403
        if is_admin:
            controllable = set(KNOWN_LIVE_MODELS)
        else:
            access = TIER_ACCESS.get(acting.get("tier", "tier1"), [])
            controllable = {m for m in KNOWN_LIVE_MODELS if MODEL_ACCESS_MODULE.get(m) in access}
        requested = body.get("modes", {})
        if not isinstance(requested, dict):
            return jsonify({"ok": False, "error": "modes must be an object"}), 400
        invalid = {m: v for m, v in requested.items()
                   if m not in KNOWN_LIVE_MODELS or v not in MODEL_MODE_VALUES}
        if invalid:
            return jsonify({"ok": False, "error": f"invalid modes: {invalid}"}), 400
        prev = get_model_modes(uid)
        # Reject any attempt to change a model outside the actor's role.
        denied = [m for m, v in requested.items() if prev.get(m) != v and m not in controllable]
        if denied:
            return jsonify({"ok": False, "error": f"your role cannot change: {denied}"}), 403
        saved = set_model_modes(requested, user_id=uid)
        # Two-way phase swap on every mode change, for BOTH the instance (uid None)
        # and per-user (multi-tenant) bundles. Going Live stashes the paper trades
        # so live starts fresh from original capital; falling back to Paper restores
        # them. Nothing is deleted — see apply_phase_transition().
        for m in KNOWN_LIVE_MODELS:
            if prev.get(m) != saved.get(m):
                apply_phase_transition(m, prev.get(m), saved.get(m), user_id=uid)
        log_automation(f"model_modes[{uid or 'instance'}] set to {saved}", level="INFO")
        return jsonify({"ok": True, "user_id": uid, "modes": get_model_modes(uid),
                        "open_positions": _open_positions_by_model(uid)})
    return jsonify({"ok": True, "user_id": uid, "modes": get_model_modes(uid),
                    "known": list(KNOWN_LIVE_MODELS), "meta": LIVE_MODEL_META,
                    "open_positions": _open_positions_by_model()})


def _broker_net_positions(user_id=None):
    """{tradingsymbol: net_quantity} from the account's ACTUAL Zerodha positions.
    Used so Kill & Exit closes what is really open, not what the records assume."""
    net = {}
    try:
        kite = get_kite(require_token=True, user_id=user_id)
        for p in (kite.positions() or {}).get("net", []):
            sym = p.get("tradingsymbol")
            if sym:
                net[sym] = int(p.get("quantity", 0) or 0)
    except Exception as e:
        log_automation(f"kill-exit: kite.positions() failed, falling back to records: {e}", level="WARNING")
    return net


def squareoff_model_open_positions(model, user_id=None):
    """Kill & Exit: close a model's open positions using the account's ACTUAL
    broker net quantities (falling back to recorded qty), then mark them CLOSED.
    Call while the model is still Live so orders are permitted."""
    key = read_bundle_key(model, user_id) if user_id else ("strategy_bundle::" + model)
    data = kv_get(key, {}) or {}
    trades = data.get("trades", []) or []
    net = _broker_net_positions(user_id)
    closed = 0
    for t in trades:
        if not (isinstance(t, dict) and str(t.get("status", "")).upper() == "OPEN"):
            continue
        try:
            if model == "futures":
                sym = t.get("symbol") or pick_nifty_futures_contract()["tradingsymbol"]
                # Prefer the real broker net; fall back to recorded direction/qty.
                if sym in net and net[sym] != 0:
                    q = net[sym]
                    place_live_order_with_retry("SELL" if q > 0 else "BUY", sym, abs(q),
                                                reason="futures_kill_exit", user_id=user_id)
                else:
                    # Broker shows NO open position for this contract — there is
                    # nothing to close. Do NOT place a blind order from the recorded
                    # qty: that would OPEN a new naked position. Fail safe: warn and
                    # let the user verify on Zerodha / close manually if needed.
                    log_automation(f"futures kill-exit: broker shows no open position for {sym} — nothing to close (already flat?). No order placed.", level="WARNING")
            else:
                # Spread / directional: close each leg by its ACTUAL broker net qty.
                any_leg = False
                for sym in (t.get("atm_tradingsymbol"), t.get("otm_tradingsymbol"), t.get("tradingsymbol")):
                    if sym and sym in net and net[sym] != 0:
                        q = net[sym]
                        place_live_order_with_retry("SELL" if q > 0 else "BUY", sym, abs(q),
                                                    reason=f"{model}_kill_exit", user_id=user_id)
                        any_leg = True
                if not any_leg:
                    # No matching OPEN broker legs — nothing to close. Do NOT place
                    # blind exit orders (they would open new naked positions). Fail
                    # safe: warn and let the user verify/close on Zerodha manually.
                    log_automation(f"{model} kill-exit: broker shows no open legs — nothing to close (already flat?). No order placed.", level="WARNING")
        except Exception as e:
            log_automation(f"{model} kill-exit error: {e}", level="ERROR")
        t["status"] = "CLOSED"
        t["exit_reason"] = "kill_exit"
        closed += 1
    data["trades"] = trades
    kv_set(key, data)
    log_automation(f"{model}: kill-exit closed {closed} recorded position(s) (broker net used where available)", level="INFO")
    return closed


@app.route("/api/model/squareoff", methods=["POST"])
def api_model_squareoff():
    body = request.get_json(force=True) or {}
    model = body.get("model")
    uid = body.get("user_id")
    if model not in KNOWN_LIVE_MODELS:
        return jsonify({"ok": False, "error": "unknown model"}), 400
    if uid and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "admin required"}), 403
    try:
        n = squareoff_model_open_positions(model, user_id=uid)
        return jsonify({"ok": True, "model": model, "closed": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Per-model metadata for the go-live board (label, strategy, capital, risk)
LIVE_MODEL_META = {
    "futures":          {"label": "Futures",              "strategy": "NIFTY Futures — directional",        "capital": "—",           "risk": "—"},
    "ob_workstation":   {"label": "OB Workstation",       "strategy": "Options Buy — directional",          "capital": "₹10,00,000",  "risk": "7%"},
    "ait_workstation":  {"label": "AIT Workstation",      "strategy": "Credit spread (ATM/OTM)",            "capital": "₹5,00,000",   "risk": "10%"},
    "nexp_workstation": {"label": "NiftyEXP Workstation", "strategy": "Weekly credit spread · Mon→Tue",     "capital": "₹5,00,000",   "risk": "12%"},
    "nifty_strangle_w": {"label": "Nifty Strangle 3.5%",  "strategy": "Short strangle 3.5% OTM · Wed 9:20→15:38", "capital": "₹10,00,000", "risk": "10%"},
}
LIVE_MODEL_LABELS = {k: v["label"] for k, v in LIVE_MODEL_META.items()}

@app.route("/go-live", methods=["GET"])
def go_live_page():
    """Execution-control page. Requires login + 'automation' access (Premium or
    Administrator). Only admins can actually change modes; Premium sees it read-only."""
    if "user_id" not in session:
        return redirect("/login")
    user = get_user_by_id(session["user_id"])
    access = TIER_ACCESS.get(user.get("tier", "tier1"), []) if user else []
    # Allow anyone who can control at least one tradeable model (Standard has
    # Futures, Premium/Admin have all). Basic (Dashboard only) is denied.
    can_trade = bool(user and (user.get("is_admin") or ({"futures", "automation"} & set(access))))
    if not can_trade:
        return "Access denied — your account has no tradeable models. Contact your administrator.", 403
    return GO_LIVE_HTML


GO_LIVE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STAIRS — Execution Control</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--card2:#263548;--bd:#334155;--tx:#f8fafc;--mut:#94a3b8;
        --paper:#3b82f6;--live:#10b981;--off:#64748b;--danger:#ef4444;--dangerh:#dc2626}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--tx);min-height:100vh;
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{display:flex;justify-content:space-between;align-items:center;padding:1rem 1.75rem;
         background:var(--card);border-bottom:1px solid var(--bd)}
  .logo{font-size:1.15rem;font-weight:700;display:flex;align-items:center;gap:.5rem}
  .user-switcher{display:flex;align-items:center;gap:.6rem;font-size:.85rem;color:var(--mut)}
  .user-switcher select{background:var(--bg);color:var(--tx);border:1px solid var(--bd);
        border-radius:8px;padding:7px 12px;font-size:.85rem}
  main{max-width:1400px;margin:0 auto;padding:1.75rem;width:100%}
  .intro{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:1.5rem}
  .intro h1{font-size:1.4rem;margin-bottom:.2rem}
  .intro p{color:var(--mut);font-size:.9rem}
  .profile-badge{background:rgba(59,130,246,.12);color:var(--paper);border:1px solid rgba(59,130,246,.3);
        padding:8px 14px;border-radius:10px;font-size:.85rem}
  .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1.5rem}
  @media(max-width:1024px){.grid{grid-template-columns:1fr}}
  .col{background:rgba(30,41,59,.4);border:1px solid var(--bd);border-radius:14px;padding:1.25rem;min-height:420px}
  .colhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.25rem;
           padding-bottom:.75rem;border-bottom:1px solid var(--bd)}
  .coltitle{font-weight:700;display:flex;align-items:center;gap:.4rem;font-size:1.05rem}
  .badge{font-size:.72rem;padding:.22rem .6rem;border-radius:999px;font-weight:700}
  .badge-off{background:rgba(100,116,139,.15);color:var(--off)}
  .badge-paper{background:rgba(59,130,246,.15);color:var(--paper)}
  .badge-live{background:rgba(16,185,129,.15);color:var(--live)}
  .list{display:flex;flex-direction:column;gap:1rem}
  .mcard{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:1.1rem;
         display:flex;flex-direction:column;gap:1rem;transition:border-color .2s}
  .mcard:hover{border-color:var(--mut)}
  .mtop{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
  .mname{font-weight:700;font-size:1rem} .mstrat{font-size:.78rem;color:var(--mut);margin-top:2px}
  .postag{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.35);
          font-size:.68rem;font-weight:700;padding:3px 8px;border-radius:999px;white-space:nowrap}
  .metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:.5rem;
           background:rgba(15,23,42,.4);padding:.7rem;border-radius:8px}
  .metric{display:flex;flex-direction:column}
  .metric .l{font-size:.65rem;color:var(--mut);text-transform:uppercase;letter-spacing:.3px}
  .metric .v{font-size:.9rem;font-weight:600}
  .ctlwrap{border-top:1px solid rgba(51,65,85,.6);padding-top:.75rem}
  .ctllabel{font-size:.7rem;color:var(--mut);text-transform:uppercase;letter-spacing:.3px;margin-bottom:.4rem;display:block}
  .seg{display:flex;background:var(--bg);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
  .seg button{flex:1;background:transparent;border:0;color:var(--mut);padding:8px 0;font-size:.8rem;
              font-weight:600;cursor:pointer;transition:.15s}
  .seg button.on-off{background:var(--off);color:#fff}
  .seg button.on-paper{background:var(--paper);color:#fff}
  .seg button.on-live{background:var(--live);color:#fff}
  .empty{text-align:center;color:var(--mut);font-style:italic;padding:1.75rem;
         border:2px dashed var(--bd);border-radius:10px}
  #msg{margin-top:1rem;font-size:.85rem;min-height:18px}
  .ok{color:var(--live)} .err{color:#fca5a5}
  /* modal */
  .overlay{position:fixed;inset:0;background:rgba(2,6,23,.7);display:none;align-items:center;justify-content:center;padding:20px;z-index:50}
  .overlay.show{display:flex}
  .modal{background:var(--card);border:1px solid var(--bd);border-radius:14px;max-width:560px;width:100%;padding:1.6rem}
  .modal h3{font-size:1.15rem;margin-bottom:.5rem}
  .modal .desc{color:var(--mut);font-size:.9rem;margin-bottom:1.1rem}
  .opt{display:flex;gap:.75rem;align-items:flex-start;background:var(--bg);border:1px solid var(--bd);
       border-radius:10px;padding:.9rem;margin-bottom:.7rem;cursor:pointer}
  .opt input{margin-top:3px}
  .opt .ot{font-weight:700;font-size:.9rem} .opt .od{font-size:.8rem;color:var(--mut);margin-top:2px}
  .opt.disabled{opacity:.45;cursor:not-allowed}
  .macts{display:flex;justify-content:flex-end;gap:.6rem;margin-top:1rem}
  .btn{border:0;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer;font-size:.85rem}
  .btn-sec{background:transparent;border:1px solid var(--bd);color:var(--tx)}
  .btn-danger{background:var(--danger);color:#fff} .btn-danger:hover{background:var(--dangerh)}
</style></head><body>
  <header>
    <div class="logo" style="display:flex;align-items:center;gap:14px">
      <a href="/" style="display:inline-flex;align-items:center;gap:6px;color:var(--tx);text-decoration:none;background:var(--card2);border:1px solid var(--bd);border-radius:8px;padding:7px 12px;font-size:13px;font-weight:600">← Back to Dashboard</a>
      <span>STAIRS — Execution Control</span>
    </div>
    <div class="user-switcher">
      <label>Active Profile</label>
      <select id="userSelect" onchange="switchUser()"><option>Loading…</option></select>
    </div>
  </header>
  <main>
    <div class="intro">
      <div>
        <h1>Model Deployments</h1>
        <p>Manage each model across Standby (off), Sandbox (paper), and Production (live).</p>
      </div>
      <div class="profile-badge" id="profileBadge">Viewing as: —</div>
    </div>
    <div class="grid">
      <div class="col">
        <div class="colhead"><div class="coltitle"><span>&#9208;</span> Standby</div><span class="badge badge-off">Trading Stopped</span></div>
        <div class="list" id="offList"></div>
      </div>
      <div class="col">
        <div class="colhead"><div class="coltitle"><span>&#129514;</span> Sandbox</div><span class="badge badge-paper">Paper Trading</span></div>
        <div class="list" id="paperList"></div>
      </div>
      <div class="col">
        <div class="colhead"><div class="coltitle"><span>&#9889;</span> Production</div><span class="badge badge-live">Live Trading</span></div>
        <div class="list" id="liveList"></div>
      </div>
    </div>
    <div id="msg"></div>
  </main>

  <div class="overlay" id="exitModal">
    <div class="modal">
      <h3 id="modalTitle">Deactivating Live Model</h3>
      <div class="desc" id="modalDesc">Choose how to handle open positions.</div>
      <label class="opt"><input type="radio" name="exitStrategy" value="kill" checked>
        <div><div class="ot">Kill &amp; Exit All Open Positions (Immediate)</div>
          <div class="od">Places market orders to close open positions now, then stops future entries.</div></div></label>
      <label class="opt" id="softOpt"><input type="radio" name="exitStrategy" value="soft">
        <div><div class="ot">Soft Stop (No New Entries)</div>
          <div class="od" id="softDesc">Leaves open positions running; blocks new signals.</div></div></label>
      <div class="macts">
        <button class="btn btn-sec" onclick="closeModal()">Cancel</button>
        <button class="btn btn-danger" onclick="confirmExitLive()">Confirm Execution Change</button>
      </div>
    </div>
  </div>
<script>
let state={modes:{},known:[],meta:{},pos:{},userId:null,isAdmin:false,access:[]};
const MODEL_ACCESS_MODULE={futures:'futures',ob_workstation:'automation',ait_workstation:'automation',nexp_workstation:'automation',nifty_strangle_w:'automation'};
function canControl(m){ return state.isAdmin || (state.access||[]).indexOf(MODEL_ACCESS_MODULE[m])!==-1; }
let pending=null;
function setMsg(t,ok){const m=document.getElementById('msg');m.textContent=t;m.className=ok?'ok':'err';}
function label(k){return (state.meta[k]&&state.meta[k].label)||k;}
function strat(k){return (state.meta[k]&&state.meta[k].strategy)||'';}
function isWindowed(k){return k==='nexp_workstation'||k==='nifty_strangle_w';}
function card(k){
  const mode=state.modes[k]||'paper';
  const meta=state.meta[k]||{};
  const posN=state.pos[k]||0;
  const c=document.createElement('div');c.className='mcard';
  const postag=(mode==='live'&&posN>0)?'<span class="postag">'+posN+' Open Trade(s)</span>':'';
  c.innerHTML=
    '<div class="mtop"><div><div class="mname">'+label(k)+'</div><div class="mstrat">'+strat(k)+'</div></div>'+postag+'</div>'+
    '<div class="metrics">'+
      '<div class="metric"><span class="l">Capital</span><span class="v">'+(meta.capital||'—')+'</span></div>'+
      '<div class="metric"><span class="l">Risk</span><span class="v">'+(meta.risk||'—')+'</span></div>'+
    '</div>'+
    '<div class="ctlwrap"><span class="ctllabel">Execution Mode</span>'+
      '<div class="seg">'+
        '<button class="'+(mode==='off'?'on-off':'')+'" data-k="'+k+'" data-m="off">Off</button>'+
        '<button class="'+(mode==='paper'?'on-paper':'')+'" data-k="'+k+'" data-m="paper">Paper</button>'+
        '<button class="'+(mode==='live'?'on-live':'')+'" data-k="'+k+'" data-m="live">Live</button>'+
      '</div></div>';
  c.querySelectorAll('.seg button').forEach(b=>b.addEventListener('click',()=>requestChange(b.dataset.k,b.dataset.m)));
  return c;
}
function render(){
  const off=document.getElementById('offList'),pap=document.getElementById('paperList'),liv=document.getElementById('liveList');
  off.innerHTML='';pap.innerHTML='';liv.innerHTML='';
  let oc=0,pc=0,lc=0;
  state.known.forEach(k=>{
    if(!canControl(k)) return;  // hide models this role can't control
    const m=state.modes[k]||'paper';
    if(m==='off'){off.appendChild(card(k));oc++;}
    else if(m==='live'){liv.appendChild(card(k));lc++;}
    else {pap.appendChild(card(k));pc++;}});
  if(!oc) off.innerHTML='<div class="empty">No models in standby.</div>';
  if(!pc) pap.innerHTML='<div class="empty">No models on paper.</div>';
  if(!lc) liv.innerHTML='<div class="empty">No models live.</div>';
}
async function postModes(newModes){
  const r=await fetch('/api/automation/model_modes',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({modes:newModes,user_id:state.userId})});
  const d=await r.json();
  if(d.ok){state.modes=d.modes;state.pos=d.open_positions||state.pos;render();return true;}
  setMsg('Save failed: '+(d.error||'unknown'),false);return false;
}
function requestChange(k,target){
  if(!canControl(k)){ setMsg('Your role cannot change '+label(k)+'.',false); return; }
  const cur=state.modes[k]||'paper';
  if(cur===target) return;
  if(cur==='live'){ // leaving live -> confirm open-position handling
    pending={k,target};
    const posN=state.pos[k]||0;
    document.getElementById('modalTitle').textContent='Deactivating "'+label(k)+'" Live Model';
    document.getElementById('modalDesc').textContent = posN>0
      ? 'This model has '+posN+' open live position(s). Choose how to handle them before switching to '+target.toUpperCase()+':'
      : 'Switch "'+label(k)+'" from Live to '+target.toUpperCase()+'? No open positions detected.';
    // For windowed models, Soft Stop can orphan a real position (scheduled exit becomes paper) -> disable it
    const softOpt=document.getElementById('softOpt');
    const softRadio=softOpt.querySelector('input');
    if(isWindowed(k)){ softOpt.classList.add('disabled'); softRadio.disabled=true;
      document.getElementById('softDesc').textContent='Not available for scheduled models (would orphan the real position).';
      document.querySelector('input[value="kill"]').checked=true; }
    else { softOpt.classList.remove('disabled'); softRadio.disabled=false;
      document.getElementById('softDesc').textContent='Leaves open positions running; blocks new signals.'; }
    document.getElementById('exitModal').classList.add('show');
  } else {
    const nm=Object.assign({},state.modes); nm[k]=target;
    postModes(nm).then(ok=>{ if(ok) setMsg(label(k)+' -> '+target.toUpperCase(),true); });
  }
}
async function confirmExitLive(){
  if(!pending) return;
  const strat=document.querySelector('input[name=exitStrategy]:checked').value;
  const {k,target}=pending;
  if(strat==='kill'){
    try{ await fetch('/api/model/squareoff',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:k,user_id:state.userId})}); }
    catch(e){}
  }
  const nm=Object.assign({},state.modes); nm[k]=target;
  const ok=await postModes(nm);
  closeModal();
  if(ok) setMsg(label(k)+' -> '+target.toUpperCase()+(strat==='kill'?' (positions killed & exited)':' (soft stop)'),true);
}
function closeModal(){document.getElementById('exitModal').classList.remove('show');pending=null;}
async function loadProfiles(){
  const sel=document.getElementById('userSelect');
  // Only an admin may change execution modes. Everyone else gets a read-only board
  // scoped to THEIR OWN per-user modes (never the shared instance modes).
  let _me=null;
  try{ _me=await (await fetch('/api/me')).json(); state.isAdmin=!!(_me&&_me.is_admin); state.access=(_me&&_me.access)||[]; }catch(e){ state.isAdmin=false; state.access=[]; }
  // Single-account (multi-tenant OFF): the engine runs on the INSTANCE modes
  // (user_id=null). The board MUST edit those, or its toggles won't control the
  // engine. Force a single "Single account" profile scoped to user_id=null.
  let mt=false;
  try{ const rm=await fetch('/api/admin/multi_tenant'); const dm=await rm.json();
    mt = !!(dm && dm.multi_tenant_enabled);
  }catch(e){}
  if(!mt){
    sel.innerHTML='<option value="">Single account (this instance)</option>';
    sel.disabled=true;
    switchUser(); return;
  }
  sel.disabled=false;
  try{
    const r=await fetch('/api/admin/users'); const d=await r.json();
    if(d && d.users && d.users.length){
      sel.innerHTML=d.users.map(u=>'<option value="'+u.id+'">'+(u.name||u.email)+(u.tier?' ('+u.tier+')':'')+'</option>').join('');
      switchUser(); return;
    }
  }catch(e){}
  // Non-admin: scope the board to THEIR OWN user id so they see their real
  // per-user modes (read-only), not the shared instance modes.
  sel.innerHTML='<option value="'+((_me&&_me.id!=null)?_me.id:'')+'">'+((_me&&_me.name)||'You')+'</option>';
  switchUser();
}
function switchUser(){
  const sel=document.getElementById('userSelect');
  const opt=sel.options[sel.selectedIndex];
  state.userId=(sel.value&&sel.value!=='')?sel.value:null;
  document.getElementById('profileBadge').innerHTML='Viewing as: <strong>'+(opt?opt.text:'')+'</strong>';
  load();  // load THIS user's modes only
  checkZerodha();
}
async function checkZerodha(){
  const badge=document.getElementById('profileBadge');
  try{const q=state.userId?('?user_id='+encodeURIComponent(state.userId)):'';
    const r=await fetch('/api/zerodha/status'+q); const d=await r.json();
    const conn = state.userId ? d.user_connected : d.connected;
    badge.innerHTML += ' &middot; Zerodha: <strong style="color:'+(conn?'#10b981':'#f59e0b')+'">'+(conn?'connected':'not connected')+'</strong>';
  }catch(e){}
}
async function load(){
  try{const q=state.userId?('?user_id='+encodeURIComponent(state.userId)):'';
    const r=await fetch('/api/automation/model_modes'+q);const d=await r.json();
    state.modes=d.modes||{}; state.known=d.known||state.known; state.meta=d.meta||state.meta; state.pos=d.open_positions||{};
    render(); setMsg('Loaded.',true);
  }catch(e){setMsg('Load failed: '+e,false);}
}
loadProfiles();
</script></body></html>"""

db_init()
# Run user management DB migration
try:
    conn = get_db(); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, google_id TEXT UNIQUE, picture TEXT, tier TEXT NOT NULL DEFAULT 'tier1', is_admin INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1, telegram_chat_id TEXT, created_at TEXT NOT NULL, last_login TEXT, expires_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS invite_codes (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, tier TEXT NOT NULL DEFAULT 'tier1', label TEXT, created_by INTEGER, used_by INTEGER, created_at TEXT NOT NULL, used_at TEXT, expires_at TEXT, max_uses INTEGER DEFAULT 1, use_count INTEGER DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS user_config (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, module TEXT NOT NULL, config_json TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_id, module))")
    cur.execute("CREATE TABLE IF NOT EXISTS user_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, module TEXT NOT NULL, trades_json TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_id, module))")
    cur.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, email TEXT, action TEXT NOT NULL, details TEXT, ip TEXT, created_at TEXT NOT NULL)")
    conn.commit(); conn.close()
except Exception as _dbe: print(f"DB migration warning: {_dbe}")
load_persisted_state()
def direct_module_store_sync_patch(signal, spot, quote, user_id=None):
    # Off (standby) means no recording/trading for Futures at all.
    if get_model_mode('futures', user_id) == 'off':
        log_automation("Futures mode OFF (standby) — signal ignored, no tracking", level="INFO")
        return
    # Multi-tenant: run futures open/flip for THIS user. place_live_order_with_retry
    # places a real order only if futures is Live for them, else records paper.
    if user_id:
        try:
            _sig = str(signal).upper()
            _pos = kv_get(_pos_cache_key(user_id), {}) or {}
            _needs_flip = any(isinstance(p, dict) and p.get("status") == "OPEN" and p.get("signal") != _sig for p in _pos.values())
            _is_new = not any(isinstance(p, dict) and p.get("status") == "OPEN" for p in _pos.values())
            if _needs_flip or _is_new:
                if _needs_flip:
                    live_squareoff_all_modules(reason="signal_flip", user_id=user_id)
                    time.sleep(2)
                live_open_all_modules(_sig, spot, quote, user_id=user_id)
        except Exception as _le:
            log_automation(f"futures per-user dispatch error for user {user_id}: {_le}", level="ERROR")
        return
    # ── Single-tenant (global) path below — unchanged ──
    # Live dispatch for Futures runs when Futures is live-enabled on the go-live
    # board. The old global DRY_RUN/LIVE switch was retired 2026-07-24.
    try:
        cfg = get_automation_config()
        mode = cfg.get("mode", "DRY_RUN") if isinstance(cfg, dict) else "DRY_RUN"
        if 'futures' in get_live_enabled_models():
            _sig = str(signal).upper()
            _pos = kv_get("dry_run_module_positions", {}) or {}
            _needs_flip = any(
                isinstance(p, dict) and p.get("status") == "OPEN" and p.get("signal") != _sig
                for p in _pos.values()
            )
            _is_new = not any(isinstance(p, dict) and p.get("status") == "OPEN" for p in _pos.values())
            if _needs_flip or _is_new:
                if _needs_flip:
                    live_squareoff_all_modules(reason="signal_flip")
                    time.sleep(2)
                live_open_all_modules(_sig, spot, quote)
            return
    except Exception as _le:
        log_automation(f"LIVE dispatch error: {_le}", level="ERROR")
        return

    today = date.today().isoformat()
    signal_time = str(master_state.get("signal_time") or now_utc_iso())

    # Load latest persisted module state so multi-worker Gunicorn stays consistent
    _refresh_module_state_from_db()

    positions = kv_get("dry_run_module_positions", {}) or {}
    if not isinstance(positions, dict):
        positions = {}

    # ── Derive a best-effort expiry even when Kite quote fails ──
    _fallback_expiry = today  # last resort
    try:
        _fallback_expiry = get_nearest_nifty_weekly_expiry()
    except Exception:
        pass

    # Build a minimal quote dict when Kite is unavailable
    # ob_quote = Options Buy quote (CE for LONG, PE for SHORT)
    # quote    = AIT/spread quote  (PE for LONG, CE for SHORT)
    ob_option_type = "CE" if signal == "LONG" else "PE"
    spread_opt_type = "PE" if signal == "LONG" else "CE"
    if not isinstance(quote, dict):
        quote = {
            "contract": {
                "tradingsymbol": "",
                "option_type": spread_opt_type,
                "expiry": _fallback_expiry,
                "strike": round_to_100(spot),
            },
            "premium": 0.0,
            "quote_timestamp": now_utc_iso(),
        }
    # Ensure contract.expiry is always populated
    if not quote.get("contract", {}).get("expiry"):
        quote.setdefault("contract", {})["expiry"] = _fallback_expiry

    def _valid_open_position(pos, rows):
        """Validate position and self-heal stale trade_index after DB reload."""
        if not isinstance(pos, dict):
            return False
        if str(pos.get("status") or "").upper() != "OPEN":
            return False
        idx = pos.get("trade_index")
        # If stored index is out of range (list reloaded from DB), scan for last OPEN row
        if not isinstance(idx, int) or idx < 0 or idx >= len(rows):
            for i in range(len(rows) - 1, -1, -1):
                row = rows[i]
                if isinstance(row, dict) and str(row.get("status") or "OPEN").upper() == "OPEN":
                    pos["trade_index"] = i  # self-heal
                    return True
            return False
        row = rows[idx]
        if not isinstance(row, dict):
            return False
        if str(row.get("status") or "OPEN").upper() != "OPEN":
            # Index exists but row is CLOSED — scan for actual last OPEN
            for i in range(len(rows) - 1, -1, -1):
                r = rows[i]
                if isinstance(r, dict) and str(r.get("status") or "OPEN").upper() == "OPEN":
                    pos["trade_index"] = i  # self-heal
                    return True
            return False
        return True

    def _safe_ltp(symbol, exchange="NFO", fallback=0.0):
        try:
            kite = get_kite(require_token=True)
            instrument_key = f"{exchange}:{symbol}"
            q = kite.quote([instrument_key])
            return float(q[instrument_key].get("last_price") or fallback or 0.0)
        except Exception:
            return float(fallback or 0.0)

    def _safe_spread(signal_value, spot_value, expiry_value, gap_value):
        try:
            return get_nifty_spread_quote(signal_value, float(spot_value), expiry_value, int(gap_value))
        except Exception as e:
            log_automation(f"SPREAD QUOTE FALLBACK: {e}", level="WARNING")
            return {
                "signal": signal_value,
                "spot": float(spot_value),
                "expiry": expiry_value or _fallback_expiry,
                "atm_strike": round_to_100(spot_value),
                "otm_strike": round_to_100(spot_value) - int(gap_value) if str(signal_value).upper() == "LONG" else round_to_100(spot_value) + int(gap_value),
                "option_type": "PE" if str(signal_value).upper() == "LONG" else "CE",
                "atm_tradingsymbol": "",
                "otm_tradingsymbol": "",
                "atm_sell_premium": 0.0,
                "otm_buy_premium": 0.0,
                "quote_time": now_utc_iso(),
                "instruments_refreshed_at": instrument_cache.get("refreshed_at"),
            }

    def _close_opt_buy(pos, exit_price):
        idx = pos["trade_index"]
        opt_buy_trades[idx].update({
            "exit_price": float(exit_price or 0.0),
            "exit_date": today,
            "status": "CLOSED",
            "exit_signal_time": signal_time,
        })

    def _close_spread(rows, pos, spread):
        idx = pos["trade_index"]
        rows[idx].update({
            "atm_exit_price": float(spread.get("atm_sell_premium") or 0.0),
            "otm_exit_price": float(spread.get("otm_buy_premium") or 0.0),
            "exit_date": today,
            "status": "CLOSED",
            "exit_signal_time": signal_time,
        })

    def _close_futures(pos, exit_price):
        idx = pos["trade_index"]
        futures_trades[idx].update({
            "partial_exit1": float(exit_price or 0.0),
            "partial_exit1_date": today,
            "status": "CLOSED",
            "exit_signal_time": signal_time,
        })

    # Always update configs so APIs/UI reflect latest TradingView signal
    try:
        futures_config["signal_source"] = "TRADINGVIEW"
        futures_config["signal_time"] = signal_time
    except Exception:
        pass

    # ========================
    # OB / AIT / NIFTY EXP now handled by models_v2 (called separately from webhook)
    # Old handler calls disabled to prevent double-invocation.
    # ========================
    pass  # models_v2.handle_main_signal does this work

    # ========================
    # FUTURES — direct DB ops
    # ========================
    try:
        pos = positions.get("futures")
        fut_data = kv_get("strategy_bundle::futures", {}) or {}
        fut_trades = fut_data.get("trades", [])
        # Always use futures price, not spot
        try:
            # Rollover logic: if expiry within 7 days, use next month contract
            _fut_contract = pick_nifty_futures_contract(use_rollover_logic=True)
            _kite = get_kite(require_token=True)
            _ltp = _kite.ltp([f"{_fut_contract['exchange']}:{_fut_contract['tradingsymbol']}"])
            entry_price = float(_ltp[f"{_fut_contract['exchange']}:{_fut_contract['tradingsymbol']}"]["last_price"])
        except Exception as _fe:
            log_automation(f"Futures LTP fallback to spot: {_fe}", level="WARNING")
            entry_price = float(spot)
        idx = pos.get("trade_index") if isinstance(pos, dict) else None
        if isinstance(idx, int) and 0 <= idx < len(fut_trades) and str(fut_trades[idx].get("status","OPEN")).upper() == "OPEN":
            if pos.get("signal") != signal:
                fut_trades[idx].update({"partial_exit1": entry_price, "partial_exit1_date": today, "status": "CLOSED", "exit_signal_time": signal_time})
                positions.pop("futures", None)
                pos = None
        else:
            positions.pop("futures", None)
            pos = None
        if not pos and _past_entry_cutoff():
            log_automation(
                f"Futures: signal {signal} received after entry cutoff (3:15 PM) — no new position opened",
                level="WARNING"
            )
            try:
                send_telegram(
                    f"⏱️ <b>Futures — entry skipped</b>\n"
                    f"Signal: {signal} | Spot: {spot}\n"
                    f"Too close to square-off (after 3:15 PM) — no new position opened."
                )
            except Exception:
                pass
        elif not pos:
            _fut_sym = _fut_contract.get("tradingsymbol", "NIFTY-FUT") if "_fut_contract" in dir() else "NIFTY-FUT"
            fut_trades.append({"date": today, "trend": signal, "entry": entry_price, "action_type": "Signal", "partial_exit1": "", "partial_exit1_date": "", "partial_exit2": "", "partial_exit2_date": "", "status": "OPEN", "entry_signal_time": signal_time, "symbol": _fut_sym})
            positions["futures"] = {"status": "OPEN", "signal": signal, "trade_index": len(fut_trades)-1, "symbol": _fut_sym}
        fut_data["trades"] = fut_trades
        kv_set("strategy_bundle::futures", fut_data)
        log_automation(f"DEBUG POST-FUTURES WRITE: {len(fut_trades)} trades in fut_data", level="INFO")
        # Verify it was written
        _verify = kv_get("strategy_bundle::futures", {}) or {}
        log_automation(f"DEBUG VERIFY FUTURES IN DB: {len(_verify.get('trades',[]))} trades", level="INFO")
    except Exception as e:
        log_automation(f"FUTURES DIRECT SYNC ERROR: {e}", level="ERROR")

    kv_set("dry_run_module_positions", positions)
    _clear_stale_last_error()
    log_automation("DIRECT MODULE STORE SYNC COMPLETED", level="INFO")



# ═══════════════════════════════════════════════════════════════════════════════
# OPTIONS BUY & OPTIONS AIT — CLEAN REWRITE  (auto-patched)
# Strategy rules only. No extra logic.
# ═══════════════════════════════════════════════════════════════════════════════

def ob_option_type_for_signal(signal):
    """Options Buy: LONG → buy CE,  SHORT → buy PE."""
    return "CE" if str(signal).upper() == "LONG" else "PE"


def ait_option_type_for_signal(signal):
    """AIT Spread sell leg: LONG → sell PE spread,  SHORT → sell CE spread."""
    return "PE" if str(signal).upper() == "LONG" else "CE"


def ait_otm_strike_for_signal(atm_strike, signal, gap=100):
    """OTM hedge: LONG (sell PE) → ATM - gap;  SHORT (sell CE) → ATM + gap."""
    return atm_strike - gap if str(signal).upper() == "LONG" else atm_strike + gap


def get_next_nifty_weekly_expiry_after(current_expiry_str):
    """Return the NEXT Tuesday after current_expiry_str so rollover always advances."""
    from datetime import datetime as _dt, timedelta as _td, date as _date
    try:
        current_dt = _dt.strptime(current_expiry_str, "%Y-%m-%d").date()
        candidate = current_dt + _td(days=1)
        while candidate.weekday() != 1:   # 1 = Tuesday
            candidate += _td(days=1)
        return candidate.isoformat()
    except Exception:
        from datetime import date as _d2, timedelta as _td2
        candidate = _d2.today() + _td2(days=7)
        while candidate.weekday() != 1:
            candidate += _td2(days=1)
        return candidate.isoformat()


def safe_ob_quote(signal, spot, expiry):
    """Live LTP for OB option; falls back to zeros if Kite unavailable."""
    opt_type = ob_option_type_for_signal(signal)
    try:
        contract = pick_nifty_option_contract_by_type(opt_type, spot, expiry)
        kite = get_kite(require_token=True)
        sym = contract["tradingsymbol"]
        ltp_data = kite.ltp([f"{contract['exchange']}:{sym}"])
        premium = float(ltp_data[f"{contract['exchange']}:{sym}"]["last_price"])
        return {
            "tradingsymbol": sym,
            "premium":       premium,
            "option_type":   opt_type,
            "expiry":        expiry,
            "strike":        contract.get("strike", round_to_100(spot)),
        }
    except Exception as _e:
        log_automation(f"OB QUOTE FALLBACK ({signal}): {_e}", level="WARNING")
        return {
            "tradingsymbol": "",
            "premium":       0.0,
            "option_type":   opt_type,
            "expiry":        expiry,
            "strike":        round_to_100(spot),
        }


def safe_spread_quote(signal, spot, expiry, gap=100):
    """Live spread quote for AIT; falls back to zeros if Kite unavailable."""
    try:
        return get_nifty_spread_quote(signal, spot, expiry, gap)
    except Exception as _e:
        log_automation(f"SPREAD QUOTE FALLBACK ({signal}): {_e}", level="WARNING")
        opt_type = ait_option_type_for_signal(signal)
        atm = round_to_100(spot)
        otm = ait_otm_strike_for_signal(atm, signal, gap)
        return {
            "signal":             signal,
            "spot":               spot,
            "expiry":             expiry,
            "option_type":        opt_type,
            "atm_strike":         atm,
            "otm_strike":         otm,
            "atm_tradingsymbol":  "",
            "otm_tradingsymbol":  "",
            "atm_sell_premium":   0.0,
            "otm_buy_premium":    0.0,
            "quote_time":         now_utc_iso(),
        }


def ob_compute_qty(capital, risk_pct, premium, lot_size=65):
    """
    OB qty = floor( (capital × risk_pct) / premium / lot_size ) × lot_size
    Returns 0 when premium is 0 (safety guard — avoids divide-by-zero crash).
    """
    if premium <= 0:
        return 0
    risk_budget = capital * risk_pct
    raw_qty = int(risk_budget / premium)
    lots = raw_qty // lot_size
    return lots * lot_size


def ait_compute_qty(capital, risk_pct, gap=100, lot_size=65):
    """
    AIT qty = floor( (capital × risk_pct) / gap / lot_size ) × lot_size
    Minimum 1 lot.
    """
    risk_budget = capital * risk_pct
    raw_qty = int(risk_budget / gap)
    lots = raw_qty // lot_size
    return max(lots * lot_size, lot_size)









# ═══════════════════════════════════════════════════════════════════════════════
# END OB & AIT REWRITE
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# NIFTY EXP — CLEAN REWRITE v3  (auto-patched)
#
# Entry    : Monday 3:15 PM — open spread on prevailing signal. No reaction after.
# Mid-week : No action on any signal change — hold position
# Tuesday  : Every signal change → close current + open new direction
# Exit     : Tuesday 3:15 PM — final square off, no rollover
# LONG     : Sell ATM PE + Buy OTM PE (ATM - 100)
# SHORT    : Sell ATM CE + Buy OTM CE (ATM + 100)
# ═══════════════════════════════════════════════════════════════════════════════



















# ═══════════════════════════════════════════════════════════════════════════════
# END NIFTY EXP REWRITE v3
# ═══════════════════════════════════════════════════════════════════════════════



@app.route("/api/nifty/weekly_expiry")
def api_nifty_weekly_expiry():
    """Return nearest NIFTY weekly expiry date (Tuesday)."""
    try:
        expiry = get_nearest_nifty_weekly_expiry()
        return jsonify({"ok": True, "expiry": expiry})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/nifty/option_ltp")
def api_nifty_option_ltp():
    """
    Fetch live LTP for a NIFTY option from Zerodha.
    Query params: strike (int), type (CE/PE), expiry (YYYY-MM-DD)
    """
    try:
        strike = int(request.args.get("strike", 0))
        opt_type = str(request.args.get("type", "CE")).upper()
        expiry = request.args.get("expiry", "")
        if not strike or not expiry:
            return jsonify({"ok": False, "error": "strike and expiry required"})
        contract = pick_nifty_option_contract_by_type(opt_type, strike, expiry)
        kite = get_kite(require_token=True)
        sym = contract["tradingsymbol"]
        ltp_data = kite.ltp([f"{contract['exchange']}:{sym}"])
        ltp = float(ltp_data[f"{contract['exchange']}:{sym}"]["last_price"])
        return jsonify({
            "ok": True,
            "ltp": ltp,
            "tradingsymbol": sym,
            "strike": strike,
            "type": opt_type,
            "expiry": expiry,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "ltp": 0})

@app.route("/api/tradingview/status")
def api_tradingview_status():
    refresh_master_state_from_db()
    return jsonify({
        "ok": True,
        "configured": bool(TRADINGVIEW_WEBHOOK_SECRET),
        "webhook_path": "/api/tradingview/webhook",
        "signal_source": master_state.get("signal_source", "MANUAL"),
        "signal_time": master_state.get("signal_time"),
        "signal_time_ist": _to_ist_display(master_state.get("signal_time")),
    })


@app.route("/api/tradingview/webhook", methods=["POST"])
def api_tradingview_webhook():
    try:
        refresh_master_state_from_db()
        payload = parse_tradingview_payload()
        secret = str(payload.get("secret") or payload.get("token") or "").strip()
        if TRADINGVIEW_WEBHOOK_SECRET and secret != TRADINGVIEW_WEBHOOK_SECRET:
            log_automation("Rejected TradingView webhook with invalid secret", level="WARNING", details={"remote_addr": request.remote_addr})
            return jsonify({"ok": False, "error": "invalid_secret"}), 403
        signal = normalize_signal(payload.get("signal") or payload.get("trend") or payload.get("side"))
        spot_raw = payload.get("close", payload.get("spot", payload.get("price")))
        if spot_raw is None:
            return jsonify({"ok": False, "error": "missing_close"}), 400
        spot = float(spot_raw)
        bar_time = payload.get("time_close") or payload.get("bar_time") or payload.get("time") or now_utc_iso()
        persist_master_state_updates({
            "nifty_spot": spot,
            "nifty_trend": signal,
            "nifty_lot_size": CURRENT_NIFTY_LOT_SIZE,
            "signal_source": "TRADINGVIEW",
            "signal_time": str(bar_time),
            "nifty_trade_date": date.today().isoformat(),
        })
        refresh_master_state_from_db()
        log_automation(
            f"TradingView signal {signal} @ {spot} ({models_v2.SIGNAL_SUPERTREND_LABEL})",
            details={"source": "TRADINGVIEW", "bar_time": bar_time, "symbol": payload.get("symbol") or payload.get("ticker"), "timeframe": payload.get("timeframe"), "supertrend": models_v2.SIGNAL_SUPERTREND_LABEL}
        )
        # Everything above this line is local bookkeeping and is already
        # done: the signal is persisted and the dashboard will show it. What
        # follows used to be quote_option + handle_main_signal + module sync,
        # inline, which took ~24s with a stale token while TradingView gave up
        # at ~3s and retried. It now goes to the queue drained by
        # position_sync_job, and we return immediately.
        ensure_sync_thread()      # idempotent; guarantees a drainer exists
        _q = enqueue_signal(signal, spot, str(bar_time))
        return jsonify({"ok": True, "signal": signal, "spot": spot,
                        "signal_time": bar_time, "queued": _q.get("queued"),
                        "duplicate": _q.get("duplicate"), "signal_id": _q.get("id")})
        # NOTE: legacy trigger_automation_from_signal removed — automation_cycle
        # was the old single-model futures loop, irrelevant in current 4-model architecture.
        return jsonify({"ok": True, "signal": signal, "spot": spot, "signal_time": bar_time})
    except Exception as e:
        log_automation(f"TradingView webhook error: {e}", level="ERROR", details={"traceback": traceback.format_exc()})
        return jsonify({"ok": False, "error": str(e)}), 500



# ═══════════════════════════════════════════════════════════════════
# LIVE AUTOMATION — Telegram, Orders, Squareoff, EOD
# ═══════════════════════════════════════════════════════════════════

import requests as _requests

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')

def send_telegram(message, chat_id=None):
    """Send to a specific chat if given (per-user), else the global chat."""
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        return
    try:
        _requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": target, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log_automation(f"Telegram send failed: {e}", level="WARNING")


def get_user_telegram_chat(user_id):
    """That app user's own Telegram chat id (they saved it), else None."""
    u = get_user_by_id(user_id) if user_id else None
    return (u.get("telegram_chat_id") if u else None) or None


def place_live_order_with_retry(transaction_type, symbol, quantity, reason="automation", max_retries=3, user_id=None):
    # Sole gate: the model must be live-enabled for this account (per-user if user_id).
    if not live_order_permitted(reason, user_id):
        log_automation(f"PAPER {transaction_type} {symbol} x{quantity} [{reason}] (model not live-enabled)")
        send_telegram(f"🔵 <b>PAPER</b> {transaction_type} {symbol} x{quantity}\nReason: {reason}")
        return {"ok": True, "dry_run": True, "order_id": f"DRYRUN-{int(time.time())}", "attempts": 1}
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            kite = get_kite(require_token=True, user_id=user_id)
            order_id = kite.place_order(
                variety="regular", exchange="NFO", tradingsymbol=symbol,
                transaction_type=transaction_type, quantity=int(quantity),
                product=AUTOMATION_PRODUCT, order_type=AUTOMATION_ORDER_TYPE,
                market_protection=AUTOMATION_MARKET_PROTECTION,
            )
            log_automation(f"LIVE ORDER OK: {transaction_type} {symbol} x{quantity} id={order_id}", details={"order_id": order_id, "reason": reason})
            emoji = "🟢" if transaction_type == "BUY" else "🔴"
            send_telegram(f"{emoji} <b>LIVE ORDER</b>\nAction: {transaction_type} {symbol}\nQty: {quantity} | OrderID: {order_id}\nReason: {reason}")
            return {"ok": True, "dry_run": False, "order_id": order_id, "attempts": attempt}
        except Exception as e:
            last_error = str(e)
            log_automation(f"Order attempt {attempt}/{max_retries} failed: {e}", level="WARNING")
            if attempt < max_retries:
                time.sleep(5)
    log_automation(f"ORDER FAILED after {max_retries} attempts: {transaction_type} {symbol} x{quantity} — {last_error}", level="ERROR")
    send_telegram(f"🚨 <b>ORDER FAILED</b> after {max_retries} attempts\nAction: {transaction_type} {symbol} x{quantity}\nError: {last_error}\n⚠️ Manual intervention required!")
    return {"ok": False, "dry_run": False, "order_id": None, "attempts": max_retries, "error": last_error}


def build_spread_symbols_from_trade(trend, spot, expiry, gap):
    try:
        opt_type = "PE" if str(trend).upper() == "LONG" else "CE"
        atm_strike = int(round(float(spot) / 100) * 100)
        otm_strike = atm_strike - int(gap) if str(trend).upper() == "LONG" else atm_strike + int(gap)
        rows = get_nfo_instruments()
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date() if expiry else None
        atm_sym = otm_sym = None
        for row in rows:
            if (row.get("name") == "NIFTY" and row.get("segment") == "NFO-OPT"
                    and row.get("instrument_type") == opt_type and row.get("expiry") == expiry_date):
                strike = int(float(row.get("strike", 0) or 0))
                if strike == atm_strike:
                    atm_sym = row["tradingsymbol"]
                if strike == otm_strike:
                    otm_sym = row["tradingsymbol"]
        return atm_sym, otm_sym
    except Exception as e:
        log_automation(f"build_spread_symbols error: {e}", level="ERROR")
        return None, None


def squareoff_spread_position(atm_sym, otm_sym, qty, module_name, reason="signal_flip"):
    if not atm_sym or not otm_sym:
        log_automation(f"SQUAREOFF {module_name}: missing symbols atm={atm_sym} otm={otm_sym}", level="ERROR")
        send_telegram(f"🚨 {module_name} squareoff failed — missing symbols")
        return {"ok": False, "error": "missing symbols"}
    log_automation(f"SQUAREOFF {module_name}: BUY {atm_sym} + SELL {otm_sym} x{qty}")
    send_telegram(f"⚡ <b>SQUAREOFF {module_name}</b>\nBUY: {atm_sym} + SELL: {otm_sym}\nQty: {qty} | Reason: {reason}")
    r1 = place_live_order_with_retry("BUY", atm_sym, qty, reason=f"{reason}_atm_close")
    time.sleep(1)
    r2 = place_live_order_with_retry("SELL", otm_sym, qty, reason=f"{reason}_otm_close")
    return {"ok": r1["ok"] and r2["ok"], "atm": r1, "otm": r2}


def fetch_order_average_price(order_id, user_id=None, retries=4, delay=1.5):
    """Poll Kite for the ACTUAL executed average price of an order.

    Every live-order call site in this file was recording the LTP quote taken
    a moment BEFORE placing the order as the trade's entry/exit price — never
    the real fill. On a MARKET order that's routinely off by some slippage,
    and it's just wrong to whatever degree the price moved between the quote
    and the actual match. This fetches the true average_price Kite reports
    once the order is COMPLETE. Returns None (caller falls back to the old
    pre-order quote) if it's a paper/dry-run order or Kite never confirms
    COMPLETE within the retry window."""
    if not order_id or str(order_id).startswith("DRYRUN"):
        return None
    for attempt in range(retries):
        try:
            kite = get_kite(require_token=True, user_id=user_id)
            history = kite.order_history(order_id)
            for h in reversed(history or []):
                if h.get("status") == "COMPLETE" and h.get("average_price"):
                    return float(h["average_price"])
        except Exception as e:
            log_automation(f"fetch_order_average_price error (attempt {attempt+1}/{retries}) "
                            f"for order {order_id}: {e}", level="WARNING")
        time.sleep(delay)
    log_automation(f"fetch_order_average_price: order {order_id} not COMPLETE after "
                    f"{retries} attempts — falling back to pre-order quote price", level="WARNING")
    return None


def live_squareoff_all_modules(reason="signal_flip", user_id=None):
    positions = kv_get(_pos_cache_key(user_id), {}) or {}
    results = {}
    today = date.today().isoformat()
    send_telegram(f"⏹ <b>SQUAREOFF ALL</b> — Reason: {reason}")

    # FUTURES
    try:
        pos = positions.get("futures")
        if isinstance(pos, dict) and pos.get("status") == "OPEN":
            idx = pos.get("trade_index", -1)
            fut_data = kv_get(_futures_bundle_key(user_id), {}) or {}
            fut_trades = fut_data.get("trades", [])
            trade = fut_trades[idx] if 0 <= idx < len(fut_trades) else {}
            cur_signal = pos.get("signal", "LONG")
            close_txn = "SELL" if cur_signal == "LONG" else "BUY"
            try:
                contract = pick_nifty_futures_contract()
                symbol = contract["tradingsymbol"]
                kite = get_kite(require_token=True, user_id=user_id)
                ltp_data = kite.ltp([f"{contract['exchange']}:{symbol}"])
                exit_price = float(ltp_data[f"{contract['exchange']}:{symbol}"]["last_price"])
            except Exception:
                symbol = pos.get("symbol", "NIFTY-FUT")
                # Do NOT fall back to the entry price. That records a trade
                # that looks like a clean breakeven and cannot afterwards be
                # told apart from a real one. None means "not known yet"; the
                # order fill below is the authoritative source, and if that
                # also fails the field is left blank rather than fabricated.
                exit_price = None
            qty = int(pos.get("qty", CURRENT_NIFTY_LOT_SIZE * 2))
            r = place_live_order_with_retry(close_txn, symbol, qty, reason=f"futures_{reason}", user_id=user_id)
            if r["ok"] and not r.get("dry_run"):
                real_exit = fetch_order_average_price(r.get("order_id"), user_id=user_id)
                if real_exit:
                    exit_price = real_exit
            if r["ok"] and 0 <= idx < len(fut_trades):
                if exit_price is None:
                    # The position IS closed at the broker — that is what
                    # r["ok"] means — but the price is genuinely unknown.
                    # Say so loudly instead of writing a plausible number.
                    log_automation(
                        f"SQUAREOFF: exit price UNKNOWN for futures trade {idx} "
                        f"(LTP and order-average both failed). Recorded blank, "
                        f"not the entry price.", level="ERROR")
                    send_telegram(
                        "\u26a0\ufe0f <b>EXIT PRICE UNKNOWN</b>\nSquare-off filled but the "
                        "fill price could not be read. Trade is CLOSED with a blank "
                        "exit \u2014 set it from the Kite order book.")
                fut_trades[idx].update({
                    "partial_exit1": "" if exit_price is None else exit_price,
                    "partial_exit1_date": today, "status": "CLOSED",
                    "exit_order_id": r.get("order_id")})
                fut_data["trades"] = fut_trades
                kv_set(_futures_bundle_key(user_id), fut_data)
                positions.pop("futures", None)
            results["futures"] = r
    except Exception as e:
        log_automation(f"SQUAREOFF FUTURES ERROR: {e}", level="ERROR")
        results["futures"] = {"ok": False, "error": str(e)}

    kv_set(_pos_cache_key(user_id), positions)
    ok_count = sum(1 for r in results.values() if isinstance(r, dict) and r.get("ok"))
    fail_count = len(results) - ok_count
    send_telegram(f"{'✅' if fail_count == 0 else '⚠️'} <b>SQUAREOFF COMPLETE</b>\nReason: {reason}\nSuccess: {ok_count} | Failed: {fail_count}")
    log_automation(f"SQUAREOFF COMPLETE reason={reason} results={results}", level="INFO")
    return results


def live_open_all_modules(signal, spot, quote, user_id=None):
    cfg = get_automation_config()
    mode = cfg.get("mode", "DRY_RUN")
    today = date.today().isoformat()
    results = {}
    active = get_active_models()
    send_telegram(f"\U0001F680 <b>OPENING POSITIONS</b>\nSignal: {signal} | Spot: {spot}\nMode: {mode}\nModels: {', '.join(active)}")

    # FUTURES
    if 'futures' in active:
        try:
            # Use rollover logic on signal entry — if expiry within 7 days use next month
            contract = pick_nifty_futures_contract(use_rollover_logic=True)
            kite = get_kite(require_token=True, user_id=user_id)
            ltp_data = kite.ltp([f"{contract['exchange']}:{contract['tradingsymbol']}"])
            fut_price = float(ltp_data[f"{contract['exchange']}:{contract['tradingsymbol']}"]["last_price"])
            fut_sym = contract["tradingsymbol"]
            qty = CURRENT_NIFTY_LOT_SIZE * int(futures_config.get("leverage", 2))
            txn = "BUY" if signal == "LONG" else "SELL"
            r = place_live_order_with_retry(txn, fut_sym, qty, reason="futures_entry", user_id=user_id)
            if r["ok"] and not r.get("dry_run"):
                real_entry = fetch_order_average_price(r.get("order_id"), user_id=user_id)
                if real_entry:
                    fut_price = real_entry
            if r["ok"]:
                fut_data = kv_get(_futures_bundle_key(user_id), {}) or {}
                fut_trades = fut_data.get("trades", [])
                positions = kv_get(_pos_cache_key(user_id), {}) or {}
                fut_trades.append({"date": today, "trend": signal, "entry": fut_price, "action_type": "Signal",
                                    "partial_exit1": "", "partial_exit1_date": "", "partial_exit2": "", "partial_exit2_date": "",
                                    "status": "OPEN", "entry_signal_time": now_utc_iso(), "symbol": fut_sym, "qty": qty,
                                    # Without this nothing the app records can
                                    # be matched against the Kite order book.
                                    "order_id": r.get("order_id")})
                positions["futures"] = {"status": "OPEN", "signal": signal, "trade_index": len(fut_trades)-1,
                                        "symbol": fut_sym, "qty": qty}
                fut_data["trades"] = fut_trades
                kv_set(_futures_bundle_key(user_id), fut_data)
                kv_set(_pos_cache_key(user_id), positions)
            results["futures"] = r
        except Exception as e:
            log_automation(f"OPEN FUTURES ERROR: {e}", level="ERROR")
            results["futures"] = {"ok": False, "error": str(e)}
    else:
        log_automation("Futures skipped (not in active models)", level="INFO")

    return results




def api_squareoff_all():
    try:
        results = live_squareoff_all_modules(reason="manual")
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/automation/squareoff_status")
def api_squareoff_status():
    positions = kv_get("dry_run_module_positions", {}) or {}
    return jsonify({"positions": positions, "mode": get_automation_config().get("mode", "DRY_RUN")})



# ══════════════════════════════════════════════════════════════════════════════
# ORB MODEL — Backend (independent from all other models)
# ══════════════════════════════════════════════════════════════════════════════

orb_config = {
    "capital": 500000,
    "lot_size": CURRENT_NIFTY_LOT_SIZE,
    "leverage": 1,
    "orb_start": "09:15",
    "orb_end": "11:15",
    "force_exit": "15:15",
}

orb_state = {
    "signal": None,
    "spot": None,
    "orb_high": None,
    "orb_low": None,
    "signal_time": None,
}















# ══════════════════════════════════════════════════════════════════════════════
# MULTI-USER SYSTEM — Google SSO + Invite Codes + Tier-based Access
# ══════════════════════════════════════════════════════════════════════════════

import hashlib
from authlib.integrations.flask_client import OAuth
from functools import wraps

GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
ADMIN_EMAIL          = os.environ.get('ADMIN_EMAIL', '')

# Tier definitions — what each tier can access
TIER_ACCESS = {
    'admin': ['dashboard', 'futures', 'finnifty_exp', 'stairs_lr', 'stairs_hr', 'payoff', 'automation', 'admin'],
    'tier3': ['dashboard', 'futures', 'finnifty_exp', 'stairs_lr', 'stairs_hr', 'payoff', 'automation'],
    'tier2': ['dashboard', 'futures', 'payoff'],
    'tier1': ['dashboard'],
}

TIER_NAMES = {
    'admin':  'Administrator',
    'tier3':  'Premium',
    'tier2':  'Standard',
    'tier1':  'Basic',
}

# ── OAuth setup ──────────────────────────────────────────────────────────────
# OAuth state needs server-side cache for multi-worker Gunicorn
from authlib.integrations.flask_client import OAuth
from cachelib import SimpleCache
oauth_cache = SimpleCache()

class OAuthCache:
    def get(self, key): return oauth_cache.get(key)
    def set(self, key, value, timeout=None): oauth_cache.set(key, value, timeout=300)
    def delete(self, key): oauth_cache.delete(key)

oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

# ── DB helpers ───────────────────────────────────────────────────────────────
def get_user_by_email(email):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_users():
    conn = get_db()
    rows = conn.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def create_user(email, name, google_id, picture, tier='tier1', is_admin=0):
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users(email,name,google_id,picture,tier,is_admin,is_active,created_at) VALUES(?,?,?,?,?,?,1,?)',
            (email, name, google_id, picture, tier, is_admin, now_utc_iso())
        )
        conn.commit()
        row = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        return dict(row)
    finally:
        conn.close()

def update_user_login(user_id, google_id, picture):
    conn = get_db()
    conn.execute('UPDATE users SET last_login=?, google_id=?, picture=? WHERE id=?',
                 (now_utc_iso(), google_id, picture, user_id))
    conn.commit()
    conn.close()

def get_invite_code(code):
    conn = get_db()
    row = conn.execute('SELECT * FROM invite_codes WHERE code=?', (code,)).fetchone()
    conn.close()
    return dict(row) if row else None

def use_invite_code(code, user_id):
    conn = get_db()
    conn.execute(
        'UPDATE invite_codes SET used_by=?, used_at=?, use_count=use_count+1 WHERE code=?',
        (user_id, now_utc_iso(), code)
    )
    conn.commit()
    conn.close()

def create_invite_code(tier, label, created_by, expires_days=90, max_uses=1):
    import secrets
    code = secrets.token_urlsafe(12)
    expires_at = None                       # pass expires_days=0/None for a non-expiring code
    if expires_days:
        from datetime import timedelta
        expires_at = (datetime.utcnow() + timedelta(days=int(expires_days))).isoformat() + 'Z'
    conn = get_db()
    conn.execute(
        'INSERT INTO invite_codes(code,tier,label,created_by,created_at,expires_at,max_uses,use_count) VALUES(?,?,?,?,?,?,?,0)',
        (code, tier, label, created_by, now_utc_iso(), expires_at, max_uses)
    )
    conn.commit()
    conn.close()
    return code

def get_all_invite_codes():
    conn = get_db()
    rows = conn.execute('SELECT * FROM invite_codes ORDER BY created_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def audit(action, user_id=None, email=None, details=None):
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        conn = get_db()
        conn.execute(
            'INSERT INTO audit_log(user_id,email,action,details,ip,created_at) VALUES(?,?,?,?,?,?)',
            (user_id, email, action, details, ip, now_utc_iso())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_user_module_trades(user_id, module):
    conn = get_db()
    row = conn.execute('SELECT trades_json FROM user_trades WHERE user_id=? AND module=?',
                       (user_id, module)).fetchone()
    conn.close()
    return json.loads(row['trades_json']) if row else []

def set_user_module_trades(user_id, module, trades):
    conn = get_db()
    conn.execute(
        'INSERT INTO user_trades(user_id,module,trades_json,updated_at) VALUES(?,?,?,?) '
        'ON CONFLICT(user_id,module) DO UPDATE SET trades_json=excluded.trades_json, updated_at=excluded.updated_at',
        (user_id, module, json.dumps(trades), now_utc_iso())
    )
    conn.commit()
    conn.close()

def get_user_module_config(user_id, module, default):
    conn = get_db()
    row = conn.execute('SELECT config_json FROM user_config WHERE user_id=? AND module=?',
                       (user_id, module)).fetchone()
    conn.close()
    return json.loads(row['config_json']) if row else default

def set_user_module_config(user_id, module, config):
    conn = get_db()
    conn.execute(
        'INSERT INTO user_config(user_id,module,config_json,updated_at) VALUES(?,?,?,?) '
        'ON CONFLICT(user_id,module) DO UPDATE SET config_json=excluded.config_json, updated_at=excluded.updated_at',
        (user_id, module, json.dumps(config), now_utc_iso())
    )
    conn.commit()
    conn.close()

# ── Decorators ───────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        user = get_user_by_id(session['user_id'])
        if not user or not user['is_active']:
            session.clear()
            return redirect('/login?error=account_inactive')
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        user = get_user_by_id(session['user_id'])
        if not user or not user['is_admin']:
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated

def tier_required(module):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect('/login')
            user = get_user_by_id(session['user_id'])
            if not user:
                return redirect('/login')
            tier = user.get('tier', 'tier1')
            allowed = TIER_ACCESS.get(tier, [])
            if module not in allowed:
                return jsonify({'error': f'This feature requires a higher subscription tier. Your tier: {TIER_NAMES.get(tier)}'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# ── Auth Routes ──────────────────────────────────────────────────────────────
@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect('/dashboard')
    error = request.args.get('error', '')
    invite = request.args.get('invite', '')
    return render_template('login.html', error=error, invite=invite,
                           google_enabled=bool(GOOGLE_CLIENT_ID))

@app.route('/auth/google')
def auth_google():
    import secrets as _sec
    invite_code = request.args.get('invite', '')
    redirect_uri = url_for('auth_google_callback', _external=True)
    # Store invite in session
    session['pending_invite'] = invite_code
    session.modified = True
    # Generate state manually and store in session
    import secrets as _sec
    state = _sec.token_urlsafe(32)
    session['oauth_state'] = state
    session.modified = True
    # Use requests_session to avoid state storage issue
    return oauth.google.authorize_redirect(redirect_uri, state=state, nonce=_sec.token_urlsafe(16))

@app.route('/auth/google/callback')
def auth_google_callback():
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get('userinfo') or oauth.google.userinfo()
        email    = userinfo['email']
        name     = userinfo.get('name', email)
        google_id = userinfo['sub']
        picture  = userinfo.get('picture', '')
        invite_code = session.pop('pending_invite', '')
        expected_state = session.pop('oauth_state', None)
        received_state = request.args.get('state', '')
        if expected_state and received_state != expected_state:
            return redirect('/login?error=auth_failed')

        # Check if user exists
        user = get_user_by_email(email)

        if user:
            # Existing user — update login
            if not user['is_active']:
                audit('login_blocked_inactive', email=email)
                return redirect('/login?error=account_inactive')
            update_user_login(user['id'], google_id, picture)
            session.permanent = True  # keep signed in across browser restarts (30 days)
            session['user_id'] = user['id']
            session['user_email'] = email
            session['user_name'] = name
            session['user_tier'] = user['tier']
            session['is_admin'] = bool(user['is_admin'])
            audit('login_success', user_id=user['id'], email=email)
            return redirect('/dashboard')

        # New user — check invite code (admin email bypasses invite)
        is_admin_email = (ADMIN_EMAIL and email.lower() == ADMIN_EMAIL.lower())
        if not invite_code and not is_admin_email:
            audit('login_no_invite', email=email)
            return redirect('/login?error=invite_required')

        # Check if this is admin email
        is_admin = 1 if (ADMIN_EMAIL and email.lower() == ADMIN_EMAIL.lower()) else 0

        if invite_code:
            invite = get_invite_code(invite_code)
            if not invite:
                return redirect('/login?error=invalid_invite')
            if invite['use_count'] >= invite['max_uses']:
                return redirect('/login?error=invite_used')
            if invite.get('expires_at'):
                try:
                    exp = datetime.fromisoformat(invite['expires_at'].replace('Z', '+00:00'))
                    if datetime.now(exp.tzinfo) > exp:
                        return redirect('/login?error=invite_expired')
                except Exception:
                    pass
            tier = 'admin' if is_admin else invite['tier']
            user = create_user(email, name, google_id, picture, tier=tier, is_admin=is_admin)
            use_invite_code(invite_code, user['id'])
        else:
            # Admin email — no invite needed
            tier = 'admin'
            user = create_user(email, name, google_id, picture, tier='admin', is_admin=1)

        session.permanent = True  # keep signed in across browser restarts (30 days)
        session['user_id'] = user['id']
        session['user_email'] = email
        session['user_name'] = name
        session['user_tier'] = tier
        session['is_admin'] = bool(is_admin)

        audit('register_success', user_id=user['id'], email=email,
              details=f'tier={tier} invite={invite_code}')

        # Send Telegram welcome if configured
        send_telegram(f"👤 New user registered: {name} ({email}) — Tier: {TIER_NAMES.get(tier, tier)}")

        return redirect('/dashboard')

    except Exception as e:
        app.logger.exception("Google auth error")
        return redirect(f'/login?error=auth_failed')

@app.route('/register')
def register_page():
    invite_code = request.args.get('code', '')
    if not invite_code:
        return render_template('login.html', error='invite_required',
                               google_enabled=bool(GOOGLE_CLIENT_ID))
    invite = get_invite_code(invite_code)
    if not invite:
        return render_template('login.html', error='invalid_invite',
                               google_enabled=bool(GOOGLE_CLIENT_ID))
    return redirect(f'/auth/google?invite={invite_code}')

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    email = session.get('user_email')
    session.clear()
    audit('logout', user_id=user_id, email=email)
    return redirect('/login')

# ── User API (for frontend) ──────────────────────────────────────────────────
@app.route('/api/me')
@login_required
def api_me():
    user = get_user_by_id(session['user_id'])
    return jsonify({
        'id': user['id'],
        'email': user['email'],
        'name': user['name'],
        'picture': user['picture'],
        'tier': user['tier'],
        'tier_name': TIER_NAMES.get(user['tier'], user['tier']),
        'is_admin': bool(user['is_admin']),
        'access': TIER_ACCESS.get(user['tier'], []),
    })

# ── Per-user module APIs ──────────────────────────────────────────────────────
@app.route('/api/user/futures', methods=['GET', 'POST'])
@login_required
def api_user_futures():
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    if 'futures' not in TIER_ACCESS.get(user['tier'], []):
        return jsonify({'error': 'Upgrade required'}), 403
    default_cfg = dict(futures_config)
    cfg = get_user_module_config(user_id, 'futures', default_cfg)
    trades = get_user_module_trades(user_id, 'futures')
    if request.method == 'POST':
        body = request.json or {}
        if 'config' in body:
            cfg.update(body['config'])
            set_user_module_config(user_id, 'futures', cfg)
        if 'trades' in body and len(body['trades']) > 0:
            trades = body['trades']
            set_user_module_trades(user_id, 'futures', trades)
    results, summary = calc_futures_trades(trades, cfg)
    return jsonify({'config': cfg, 'trades': results, 'summary': summary})





# ── Admin APIs ───────────────────────────────────────────────────────────────
@app.route('/admin')
@login_required
def admin_page():
    user = get_user_by_id(session['user_id'])
    if not user or not user['is_admin']:
        return redirect('/dashboard')
    return render_template('admin.html')

@app.route('/api/admin/users')
@admin_required
def api_admin_users():
    users = get_all_users()
    return jsonify({'users': users})

@app.route('/api/admin/users/<int:user_id>', methods=['PATCH'])
@admin_required
def api_admin_update_user(user_id):
    body = request.json or {}
    allowed_fields = ['tier', 'is_active', 'telegram_chat_id', 'expires_at']
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        return jsonify({'error': 'No valid fields'}), 400
    set_clause = ', '.join(f'{k}=?' for k in updates)
    values = list(updates.values()) + [user_id]
    conn = get_db()
    conn.execute(f'UPDATE users SET {set_clause} WHERE id=?', values)
    conn.commit()
    conn.close()
    audit('admin_update_user', user_id=session['user_id'],
          email=session.get('user_email'),
          details=f'updated user {user_id}: {updates}')
    return jsonify({'ok': True})

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def api_admin_delete_user(user_id):
    # Guard: an admin cannot delete their own account (avoids locking yourself out).
    if user_id == session.get('user_id'):
        return jsonify({'error': 'You cannot delete your own account.'}), 400
    conn = get_db()
    row = conn.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    deleted_email = row[0]
    conn.execute('DELETE FROM users WHERE id=?', (user_id,))
    conn.commit()
    conn.close()
    audit('admin_delete_user', user_id=session['user_id'],
          email=session.get('user_email'),
          details=f'DELETED user {user_id} ({deleted_email})')
    return jsonify({'ok': True})

@app.route('/api/admin/invite_codes', methods=['GET'])
@admin_required
def api_admin_get_invites():
    codes = get_all_invite_codes()
    return jsonify({'codes': codes})

@app.route('/api/admin/invite_codes', methods=['POST'])
@admin_required
def api_admin_create_invite():
    body = request.json or {}
    tier = body.get('tier', 'tier1')
    label = body.get('label', '')
    _ed = body.get('expires_days')
    expires_days = int(_ed) if _ed not in (None, '', 0, '0') else None  # blank / 0 = never expires
    max_uses = int(body.get('max_uses', 1))
    code = create_invite_code(tier, label, session['user_id'], expires_days, max_uses)
    invite_url = f"https://stairstomillionaire.com/register?code={code}"
    audit('admin_create_invite', user_id=session['user_id'],
          email=session.get('user_email'),
          details=f'tier={tier} label={label} code={code}')
    return jsonify({'ok': True, 'code': code, 'invite_url': invite_url})

@app.route('/api/admin/invite_codes/<int:code_id>', methods=['DELETE'])
@admin_required
def api_admin_delete_invite(code_id):
    conn = get_db()
    conn.execute('DELETE FROM invite_codes WHERE id=?', (code_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/admin/audit_log')
@admin_required
def api_admin_audit_log():
    limit = int(request.args.get('limit', 100))
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)
    ).fetchall()
    conn.close()
    return jsonify({'logs': [dict(r) for r in rows]})

@app.route('/api/admin/stats')
@admin_required
def api_admin_stats():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    active = conn.execute('SELECT COUNT(*) FROM users WHERE is_active=1').fetchone()[0]
    by_tier = conn.execute('SELECT tier, COUNT(*) as cnt FROM users GROUP BY tier').fetchall()
    conn.close()
    return jsonify({
        'total_users': total,
        'active_users': active,
        'by_tier': [dict(r) for r in by_tier],
    })


# ══════════════════════════════════════════════════════════════════════════════
# MODEL SELECTION — Per-user and admin-configurable active models
# ══════════════════════════════════════════════════════════════════════════════

ALL_MODELS = ['futures']
DEFAULT_ACTIVE_MODELS = ['futures']  # legacy orb/options_buy/options_ait/nifty_exp retired 2026-07-24

def get_active_models():
    """Get currently active models from DB."""
    saved = kv_get('active_models', None)
    if isinstance(saved, list):
        return saved
    return list(DEFAULT_ACTIVE_MODELS)

def set_active_models(models):
    """Save active models to DB."""
    valid = [m for m in models if m in ALL_MODELS]
    kv_set('active_models', valid)
    return valid

def is_model_active(model_name):
    """Check if a specific model is active."""
    return model_name in get_active_models()


@app.route('/api/automation/models', methods=['GET'])
def api_get_active_models():
    """Get active model selection."""
    return jsonify({
        'active_models': get_active_models(),
        'all_models': ALL_MODELS,
    })

@app.route('/api/automation/models', methods=['POST'])
@login_required
def api_set_active_models():
    """Set active model selection."""
    body = request.json or {}
    models = body.get('models', DEFAULT_ACTIVE_MODELS)
    saved = set_active_models(models)
    log_automation(f"Active models updated: {saved}", level="INFO",
                   details={"user": session.get("user_email"), "models": saved})
    send_telegram(f"⚙️ <b>Model Selection Updated</b>\nActive: {', '.join(saved)}")
    return jsonify({'ok': True, 'active_models': saved})



# ══════════════════════════════════════════════════════════════════════════════
# FIX 4: EOD LIMIT ORDER at ₹0.05 with market fallback at 3:37 PM
# Shifted from 3:15/3:27 PM to 3:25/3:37 PM — NSE moved the F&O close from
# 3:30 PM to 3:40 PM effective 3 Aug 2026 (Closing Auction Session rollout).
# ══════════════════════════════════════════════════════════════════════════════

def place_limit_order_at_005(symbol, qty, reason="eod_limit"):
    """Place SELL limit order at 0.05 for options. Returns order_id."""
    # Sole gate: the model must be live-enabled (see live_enabled_models / go-live board)
    if not live_order_permitted(reason):
        log_automation(f"PAPER LIMIT SELL {symbol} x{qty} @ 0.05 [{reason}] (model not live-enabled)")
        send_telegram(f"🔵 <b>PAPER</b> LIMIT SELL {symbol} x{qty} @ ₹0.05\nReason: {reason}")
        return {"ok": True, "dry_run": True, "order_id": f"DRYRUN-LIMIT-{int(time.time())}", "price": 0.05}
    try:
        kite = get_kite(require_token=True)
        order_id = kite.place_order(
            variety="regular", exchange="NFO", tradingsymbol=symbol,
            transaction_type="SELL", quantity=int(qty),
            product=AUTOMATION_PRODUCT, order_type="LIMIT", price=0.05,
        )
        log_automation(f"LIMIT ORDER 0.05 OK: SELL {symbol} x{qty} id={order_id}", details={"order_id": order_id})
        send_telegram(f"🟡 <b>LIMIT ORDER @ ₹0.05</b>\nSELL {symbol} x{qty}\nOrderID: {order_id}\nReason: {reason}")
        return {"ok": True, "dry_run": False, "order_id": order_id, "price": 0.05}
    except Exception as e:
        log_automation(f"LIMIT ORDER 0.05 FAILED: {symbol} — {e}", level="ERROR")
        return {"ok": False, "error": str(e)}


def cancel_order_if_open(order_id):
    """Cancel an open order by order_id."""
    try:
        kite = get_kite(require_token=True)
        orders = kite.orders()
        for o in orders:
            if str(o.get("order_id")) == str(order_id) and o.get("status") in ("OPEN", "TRIGGER PENDING"):
                kite.cancel_order(variety="regular", order_id=order_id)
                log_automation(f"Cancelled unfilled limit order {order_id}")
                return True
        return False
    except Exception as e:
        log_automation(f"Cancel order error: {e}", level="WARNING")
        return False


def is_limit_order_filled(order_id):
    """Check if a limit order was filled."""
    try:
        kite = get_kite(require_token=True)
        orders = kite.orders()
        for o in orders:
            if str(o.get("order_id")) == str(order_id):
                return o.get("status") == "COMPLETE"
        return False
    except Exception:
        return False


_eod_sq_done = {}
_eod_limit_orders = {}  # track pending limit orders {module: order_id}

def eod_squareoff_job():
    """
    EOD square-off job:
    - 3:25 PM: Place LIMIT SELL orders at ₹0.05 for options positions
    - 3:37 PM: Check if filled; if not, cancel and place MARKET order
    - Options only — futures held overnight
    """
    global _eod_sq_done, _eod_limit_orders
    while True:
        try:
            now = datetime.now(APP_TZ)
            today_str = now.date().isoformat()
            hhmm = now.hour * 100 + now.minute

            if now.weekday() < 5:
                cfg = get_automation_config()
                is_live = cfg.get("mode") == "LIVE" and not automation_state.get("kill_switch")

                # 3:25 PM — Place limit orders at 0.05
                if hhmm == 1525 and _eod_sq_done.get(f"{today_str}_limit") != True:
                    _eod_sq_done[f"{today_str}_limit"] = True
                    _eod_limit_orders = {}
                    positions = kv_get("dry_run_module_positions", {}) or {}
                    _open_mods = [m for m in ["options_buy", "options_ait", "nifty_exp"]
                                  if isinstance(positions.get(m), dict) and positions.get(m, {}).get("status") == "OPEN"]
                    if _open_mods:  # legacy models retired — notify/act only if any are genuinely open
                        log_automation("EOD 3:25 PM — placing limit orders at ₹0.05", level="INFO")
                        send_telegram("⏰ <b>EOD 3:25 PM</b>\nPlacing limit SELL orders at ₹0.05 for open options positions")

                    for mod in _open_mods:
                        pos = positions.get(mod)
                        if not isinstance(pos, dict) or pos.get("status") != "OPEN":
                            continue
                        try:
                            if mod == "options_buy":
                                sym = pos.get("symbol", "")
                                qty = int(pos.get("qty", CURRENT_NIFTY_LOT_SIZE))
                                r = place_limit_order_at_005(sym, qty, reason=f"eod_limit_{mod}")
                                if r.get("ok"):
                                    _eod_limit_orders[mod] = {"order_id": r.get("order_id"), "type": "single", "symbol": sym, "qty": qty}
                            elif mod in ["options_ait", "nifty_exp"]:
                                atm_sym = pos.get("atm_symbol", "")
                                otm_sym = pos.get("otm_symbol", "")
                                qty = int(pos.get("qty", CURRENT_NIFTY_LOT_SIZE))
                                # For spreads: BUY back ATM (which was sold), SELL OTM (which was bought)
                                # ATM was SELL → close with BUY at market (already near 0)
                                # OTM was BUY → close with SELL at 0.05 limit
                                r_otm = place_limit_order_at_005(otm_sym, qty, reason=f"eod_limit_{mod}_otm")
                                if r_otm.get("ok"):
                                    _eod_limit_orders[mod] = {"order_id_otm": r_otm.get("order_id"), "type": "spread",
                                                               "atm_symbol": atm_sym, "otm_symbol": otm_sym, "qty": qty}
                        except Exception as e:
                            log_automation(f"EOD limit order error {mod}: {e}", level="ERROR")

                # 3:37 PM — Check fills, market fallback for unfilled
                elif hhmm == 1537 and _eod_sq_done.get(f"{today_str}_market") != True:
                    _eod_sq_done[f"{today_str}_market"] = True
                    positions = kv_get("dry_run_module_positions", {}) or {}
                    today = now.date().isoformat()
                    _open_mods = [m for m in ["options_buy", "options_ait", "nifty_exp"]
                                  if isinstance(positions.get(m), dict) and positions.get(m, {}).get("status") == "OPEN"]
                    if _open_mods:  # legacy models retired — notify/act only if any are genuinely open
                        log_automation("EOD 3:37 PM — checking limit fills, market fallback for unfilled", level="INFO")
                        send_telegram("⏰ <b>EOD 3:37 PM</b>\nChecking limit order fills — market fallback for unfilled")

                    for mod in _open_mods:
                        pos = positions.get(mod)
                        if not isinstance(pos, dict) or pos.get("status") != "OPEN":
                            continue
                        lim = _eod_limit_orders.get(mod, {})
                        try:
                            if lim.get("type") == "single":
                                filled = is_limit_order_filled(lim["order_id"]) if is_live else True
                                if not filled:
                                    cancel_order_if_open(lim["order_id"])
                                    # Market fallback
                                    r = place_live_order_with_retry("SELL", lim["symbol"], lim["qty"], reason=f"eod_market_fallback_{mod}")
                                    send_telegram(f"⚡ Market fallback for {mod}: limit unfilled → market SELL")
                                else:
                                    send_telegram(f"✅ {mod} limit order at ₹0.05 filled!")
                                # Update trade record
                                bundle_key = f"strategy_bundle::{mod}"
                                bundle = kv_get(bundle_key, {}) or {}
                                trades = bundle.get("trades", [])
                                idx = pos.get("trade_index", -1)
                                if 0 <= idx < len(trades):
                                    trades[idx].update({"exit_price": 0.05 if filled else "market", "exit_date": today, "status": "CLOSED"})
                                    bundle["trades"] = trades
                                    kv_set(bundle_key, bundle)
                                positions.pop(mod, None)

                            elif lim.get("type") == "spread":
                                otm_filled = is_limit_order_filled(lim["order_id_otm"]) if is_live else True
                                if not otm_filled:
                                    cancel_order_if_open(lim["order_id_otm"])
                                    place_live_order_with_retry("SELL", lim["otm_symbol"], lim["qty"], reason=f"eod_market_fallback_{mod}_otm")
                                    send_telegram(f"⚡ Market fallback for {mod} OTM leg")
                                # ATM leg — always market (it was sold, close with BUY)
                                r_atm = place_live_order_with_retry("BUY", lim["atm_symbol"], lim["qty"], reason=f"eod_close_{mod}_atm")
                                bundle_key = f"strategy_bundle::{mod}"
                                bundle = kv_get(bundle_key, {}) or {}
                                trades = bundle.get("trades", [])
                                idx = pos.get("trade_index", -1)
                                if 0 <= idx < len(trades):
                                    trades[idx].update({"exit_date": today, "status": "CLOSED"})
                                    bundle["trades"] = trades
                                    kv_set(bundle_key, bundle)
                                positions.pop(mod, None)

                        except Exception as e:
                            log_automation(f"EOD market fallback error {mod}: {e}", level="ERROR")

                    kv_set("dry_run_module_positions", positions)
                    log_automation("EOD square-off complete", level="INFO")
                    send_telegram("✅ <b>EOD COMPLETE</b>\nOptions closed. Futures held overnight.")

        except Exception as e:
            log_automation(f"EOD job error: {e}", level="ERROR")
        time.sleep(30)


# ══════════════════════════════════════════════════════════════════════════════
# FIX 5: POSITION SYNC — reconcile automation state with actual Zerodha positions
# ══════════════════════════════════════════════════════════════════════════════

def sync_positions_with_zerodha():
    """
    Compare automation's tracked positions with actual Zerodha positions.
    If a position was manually closed in Zerodha, update automation state.
    Returns sync report.
    """
    try:
        kite = get_kite(require_token=True)
        zerodha_positions = kite.positions()
        net_positions = zerodha_positions.get("net", [])
        # Build map of symbol -> net quantity in Zerodha
        zd_map = {}
        for p in net_positions:
            sym = p.get("tradingsymbol", "")
            qty = int(p.get("quantity", 0))
            if qty != 0:
                zd_map[sym] = qty
    except Exception as e:
        log_automation(f"Position sync: Zerodha fetch failed — {e}", level="WARNING")
        return {"ok": False, "error": str(e)}

    positions = kv_get("dry_run_module_positions", {}) or {}
    today = date.today().isoformat()
    synced = []
    alerts = []

    for mod, pos in list(positions.items()):
        if not isinstance(pos, dict) or pos.get("status") != "OPEN":
            continue

        sym = pos.get("symbol") or pos.get("atm_symbol")
        if not sym:
            continue

        zd_qty = zd_map.get(sym, 0)
        auto_qty = int(pos.get("qty", 0))
        signal = pos.get("signal", "LONG")

        # Check if position is flat in Zerodha but open in automation
        expected_sign = 1 if signal == "LONG" else -1
        if zd_qty == 0 or (zd_qty * expected_sign < 0):
            # Position was manually closed or reversed
            log_automation(f"SYNC: {mod} position closed manually in Zerodha (auto qty={auto_qty}, zd qty={zd_qty})", level="INFO")
            # Close in automation
            bundle_key = f"strategy_bundle::{mod}"
            bundle = kv_get(bundle_key, {}) or {}
            trades = bundle.get("trades", [])
            idx = pos.get("trade_index", -1)
            if 0 <= idx < len(trades):
                trades[idx].update({"exit_date": today, "status": "CLOSED", "exit_price": "manual"})
                bundle["trades"] = trades
                kv_set(bundle_key, bundle)
            positions.pop(mod, None)
            synced.append(mod)
            alerts.append(f"🔄 <b>SYNC</b>: {mod} — manual exit detected in Zerodha. Automation updated.")

    if synced:
        kv_set("dry_run_module_positions", positions)
        for alert in alerts:
            send_telegram(alert)
        log_automation(f"Position sync complete. Synced: {synced}", level="INFO")
    
    # Check for unknown Zerodha positions
    auto_symbols = set()
    for pos in positions.values():
        if isinstance(pos, dict):
            for key in ["symbol", "atm_symbol", "otm_symbol"]:
                if pos.get(key):
                    auto_symbols.add(pos[key])
    
    unknown = [sym for sym in zd_map if sym not in auto_symbols and "NIFTY" in sym]
    if unknown:
        msg = f"⚠️ <b>Unknown NIFTY positions in Zerodha:</b> {', '.join(unknown)}"
        send_telegram(msg)
        log_automation(f"Unknown Zerodha positions: {unknown}", level="WARNING")

    return {"ok": True, "synced": synced, "unknown": unknown, "zerodha_positions": list(zd_map.keys())}


_zerodha_precheck_done = {}

def check_users_missing_zerodha_before_open():
    """08:45 AM IST, weekdays — for every active user who has at least one
    model that isn't Off but hasn't connected their OWN Zerodha account, send
    THEM (not just the admin) a personal Telegram alert so they can reconnect
    before the 09:15 open.

    Widened from Live-only to Live-or-Paper: in this app PAPER trading still
    depends on a live Zerodha token — spot/premium quotes, option contract
    lookups, everything is sourced from Kite, and Live vs Paper only decides
    whether a REAL order gets placed on top of that. A missing token silently
    breaks Paper simulation too (no trades get recorded at all, not just no
    real orders), so Paper-only users need this warning just as much.

    Kite access tokens expire daily (~6 AM), so this needs a fresh reconnect
    every single morning — there's no "set it once" here.
    Idempotent — fires at most once per user per day."""
    now = datetime.now(APP_TZ)
    if now.weekday() >= 5:
        return
    # Was `now.minute != 45`, which needed a loop tick to land exactly on
    # that minute. The driving loop sleeps a fixed interval from process
    # start, so whether this ever ran was decided by the second of the last
    # restart. At-or-after fires on the first tick past the target for any
    # interval; _zerodha_precheck_done keeps it to once a day.
    if (now.hour * 60 + now.minute) < (8 * 60 + 45):
        return
    today_str = now.date().isoformat()
    if _zerodha_precheck_done.get(today_str):
        return
    _zerodha_precheck_done[today_str] = True

    try:
        for u in get_all_users():
            uid = u.get("id")
            if not uid or not u.get("is_active"):
                continue
            modes = get_model_modes(uid)
            if not any(m in ("live", "paper") for m in modes.values()):
                continue  # every model Off — no Zerodha needed today
            if get_access_token(uid):
                continue  # connected — nothing to warn about
            name = (u.get("name") or "").split(" ")[0] or "there"
            live_models = [m for m, mode in modes.items() if mode == "live"]
            urgency = "Your LIVE model(s) will NOT place real orders" if live_models \
                else "Your Paper model(s) will NOT record any trades"
            msg = (f"⚠️ <b>Zerodha not connected</b>\nHi {name} — market opens in ~30 min "
                   f"and your Zerodha isn't connected. {urgency} until you reconnect on "
                   f"the STAIRS dashboard. (Kite tokens expire every day — this needs a "
                   f"fresh reconnect each morning.)")
            cid = get_user_telegram_chat(uid)
            # A missing PERSONAL chat id must not mean silence. send_telegram()
            # falls back to the global TELEGRAM_CHAT_ID when chat_id is None,
            # so the admin still hears it. The old `if cid:` discarded the
            # message entirely — that is how the 10 Aug stale token went
            # unreported while the alert text sat composed in memory.
            send_telegram(
                msg if cid else f"\u26a0\ufe0f (no personal Telegram id for user {uid})\n{msg}",
                chat_id=cid)
            log_automation(
                f"Zerodha pre-open check: user {uid} ({u.get('email')}) has active model(s) "
                f"but no Zerodha token" + ("" if cid else " (no Telegram chat id on file — could not notify them directly)"),
                level="WARNING", details={"user_id": uid, "email": u.get("email")})
    except Exception as e:
        log_automation(f"check_users_missing_zerodha_before_open ERROR: {e}", level="ERROR")



_token_health_done = {}

def check_kite_token_health():
    """10:00 AM IST, weekdays — verify each active user's Kite token actually
    WORKS, not merely that one is stored.

    check_users_missing_zerodha_before_open() at 08:45 tests presence only. An
    expired token still returns a value from get_access_token(), so that check
    passes while every subsequent Kite call fails. This calls kite.profile()
    to confirm the token is live. Timed at 10:00, ahead of the 10:15 first
    signal window. Idempotent — at most once per day."""
    now = datetime.now(APP_TZ)
    if now.weekday() >= 5:
        return
    # Same defect as the 08:45 check: an exact-minute test against a loop
    # that ticks on an arbitrary offset. _token_health_done keeps it once
    # per day; this only decides WHEN the first qualifying tick counts.
    if (now.hour * 60 + now.minute) < (10 * 60 + 0):
        return
    today_str = now.date().isoformat()
    if _token_health_done.get(today_str):
        return
    _token_health_done[today_str] = True

    try:
        for u in get_all_users():
            uid = u.get("id")
            if not uid or not u.get("is_active"):
                continue
            modes = get_model_modes(uid)
            if not any(m in ("live", "paper") for m in modes.values()):
                continue
            if not get_access_token(uid):
                continue  # no token at all — the 08:45 check covers that

            err = None
            try:
                # user_id form only: there is no Flask session in this thread
                get_kite(require_token=True, user_id=uid).profile()
            except Exception as e:
                err = str(e)[:200]
            if err is None:
                continue

            name = (u.get("name") or "").split(" ")[0] or "there"
            live_models = [m for m, mode in modes.items() if mode == "live"]
            urgency = ("Your LIVE model(s) will NOT place real orders"
                       if live_models else "Your Paper model(s) will NOT record any trades")
            msg = (f"\U0001F534 <b>Zerodha token EXPIRED</b>\nHi {name} — a token is saved "
                   f"but Zerodha is rejecting it. {urgency} until you reconnect on the "
                   f"STAIRS dashboard. First signal window is 10:15.")
            cid = get_user_telegram_chat(uid)
            # A missing PERSONAL chat id must not mean silence. send_telegram()
            # falls back to the global TELEGRAM_CHAT_ID when chat_id is None,
            # so the admin still hears it. The old `if cid:` discarded the
            # message entirely — that is how the 10 Aug stale token went
            # unreported while the alert text sat composed in memory.
            send_telegram(
                msg if cid else f"\u26a0\ufe0f (no personal Telegram id for user {uid})\n{msg}",
                chat_id=cid)
            log_automation(
                f"Kite token health: user {uid} ({u.get('email')}) token is STALE "
                f"(profile() failed: {err})"
                + ("" if cid else " (no Telegram chat id on file)"),
                level="ERROR",
                details={"user_id": uid, "email": u.get("email"), "error": err})
    except Exception as e:
        log_automation(f"check_kite_token_health ERROR: {e}", level="ERROR")



# ── signal queue ───────────────────────────────────────────────────────────
# The TradingView webhook used to do the broker work inline and took ~24s with
# a stale token, against TradingView's ~3s patience. It now records the signal
# here and returns; position_sync_job drains this between its naps.
#
# CONCURRENCY: the webhook (gunicorn worker thread) appends and the sync thread
# removes, so both sides take automation_lock around the read-modify-write.
# That is sufficient because gunicorn runs workers=1 — one process, so one
# lock. If workers is ever raised above 1, this needs a DB-level guard instead;
# an RLock does not span processes.
PENDING_SIGNALS_KEY = "pending_signals"
PROCESSED_SIGNALS_KEY = "processed_signal_ids"
PROCESSED_HISTORY = 200


def _signal_fingerprint(signal, spot, bar_time):
    """Identity of a signal. A TradingView retry repeats the same bar, so the
    same fingerprint, which is what makes the duplicate check work."""
    return "%s|%s|%s" % (signal, spot, bar_time)


def enqueue_signal(signal, spot, bar_time):
    """Record a signal for the loop thread. Returns a dict describing what
    happened — the webhook passes it straight back to TradingView."""
    fp = _signal_fingerprint(signal, spot, bar_time)
    with automation_lock:
        done = kv_get(PROCESSED_SIGNALS_KEY, []) or []
        if fp in done:
            log_automation("Duplicate signal ignored (already processed): %s" % fp,
                           level="WARNING")
            return {"queued": False, "duplicate": True, "id": fp}
        pend = kv_get(PENDING_SIGNALS_KEY, []) or []
        if any(p.get("id") == fp for p in pend):
            log_automation("Duplicate signal ignored (already queued): %s" % fp,
                           level="WARNING")
            return {"queued": False, "duplicate": True, "id": fp}
        pend.append({"id": fp, "signal": signal, "spot": spot,
                     "bar_time": str(bar_time), "queued_at": now_utc_iso()})
        kv_set(PENDING_SIGNALS_KEY, pend)
        depth = len(pend)
    log_automation("Signal QUEUED %s @ %s (queue depth %d)" % (signal, spot, depth),
                   level="INFO")
    if depth > 3:
        # Depth should be 0 or 1. Anything more means the drainer is not
        # keeping up, or is not running at all.
        try:
            send_telegram("\u26a0\ufe0f <b>SIGNAL QUEUE BACKING UP</b>\n"
                          "%d signals waiting. The sync thread may be stuck." % depth)
        except Exception:
            pass
    return {"queued": True, "duplicate": False, "id": fp}


def _execute_signal(item):
    """The broker work that used to run inside the request.

    Copied from the old inline body so behaviour is unchanged — same calls,
    same order, same per-stage exception handling. Only the thread differs.
    Returns a list of stage failures, empty when everything worked."""
    signal = item["signal"]
    spot = float(item["spot"])
    bar_time = item["bar_time"]
    failures = []

    quote = None
    try:
        quote = quote_option(signal, spot)
        log_automation("WEBHOOK QUOTE OK", level="INFO")
    except Exception as e:
        log_automation("WEBHOOK QUOTE ERROR (fallback mode): %s" % e, level="WARNING")

    try:
        import sys as _sys
        _selfmod = _sys.modules[__name__]
        if multi_tenant_enabled():
            for _uid in active_trading_users():
                try:
                    models_v2.handle_main_signal(
                        _UserScopedApp(_selfmod, _uid), signal, spot, str(bar_time)
                    )
                except Exception as _ue:
                    log_automation(
                        f"MODELS_V2 main signal ERROR for user {_uid}: {_ue}",
                        level="ERROR",
                    )
            log_automation(
                f"MODELS_V2 OB+AIT dispatched multi-tenant ({models_v2.SIGNAL_SUPERTREND_LABEL})",
                level="INFO",
            )
        else:
            models_v2.handle_main_signal(_selfmod, signal, spot, str(bar_time))
            log_automation(
                f"MODELS_V2 OB+AIT dispatched ({models_v2.SIGNAL_SUPERTREND_LABEL})",
                level="INFO",
            )
        # Strategy instances that share the SuperTrend webhook
        try:
            for _st in get_strategies():
                if _st.get("webhook") == "existing" and _st.get("mode", "off") != "off" \
                   and _st.get("type") in ("ob_workstation", "ait_workstation", "nexp_workstation"):
                    try:
                        models_v2._process_model_signal(
                            _StrategyScopedApp(_selfmod, _st),
                            _st["type"], signal, spot, str(bar_time),
                        )
                    except Exception as _se:
                        log_automation(
                            f"strategy instance signal ERROR [{_st.get('id')}]: {_se}",
                            level="ERROR",
                        )
        except Exception:
            pass
    except Exception as e:
        log_automation("MODELS_V2 main signal ERROR: %s" % e, level="ERROR")
        failures.append("models_v2.handle_main_signal: %s" % e)

    # ── module sync, on a watchdog ─────────────────────────────────────
    # This stage is the slow one: 29-32s when measured, 5s on a good day.
    # It used to run inline on position_sync_job's thread, which also runs
    # the 08:45 pre-open Zerodha check and the 10:00 token health check. A
    # slow Kite day therefore delayed the very alerts that exist to tell you
    # Kite is having a slow day.
    #
    # The call is NOT killed on timeout. Python cannot interrupt a thread
    # safely, and a broker request aborted mid-flight is worse than a slow
    # one — you would not know whether the order reached the exchange. The
    # work always finishes; the scheduler simply stops waiting for it.
    MODULE_SYNC_TIMEOUT = 20

    def _run_module_sync():
        try:
            if multi_tenant_enabled():
                for _uid in active_trading_users("futures"):
                    try:
                        direct_module_store_sync_patch(signal, spot, quote, user_id=_uid)
                    except Exception as _ue:
                        log_automation("futures sync ERROR for user %s: %s" % (_uid, _ue),
                                       level="ERROR")
                        _sync_failures.append("futures sync user %s: %s" % (_uid, _ue))
            else:
                direct_module_store_sync_patch(signal, spot, quote)
            log_automation("MODULE SYNC EXECUTED (futures)", level="INFO")
        except Exception as e:
            log_automation("MODULE SYNC ERROR: %s" % e, level="ERROR")
            _sync_failures.append("module sync: %s" % e)

    _sync_failures = []
    import threading as _th
    _t = _th.Thread(target=_run_module_sync, name="module-sync", daemon=True)
    _t0 = time.time()
    _t.start()
    _t.join(MODULE_SYNC_TIMEOUT)
    if _t.is_alive():
        # Still running. Report and move on — the scheduler must not wait.
        log_automation(
            "MODULE SYNC still running after %ss for %s @ %s — releasing the "
            "scheduler thread; the sync continues in the background."
            % (MODULE_SYNC_TIMEOUT, signal, spot), level="ERROR")
        try:
            send_telegram(
                "\u23f3 <b>MODULE SYNC SLOW</b>\n%s @ %s took longer than %ss.\n"
                "It is still running. Orders may still be placed. The 08:45 and "
                "10:00 token checks are no longer blocked behind it."
                % (signal, spot, MODULE_SYNC_TIMEOUT))
        except Exception:
            pass
        failures.append("module sync exceeded %ss (still running)"
                        % MODULE_SYNC_TIMEOUT)
    else:
        log_automation("MODULE SYNC finished in %.1fs" % (time.time() - _t0),
                       level="INFO")
        failures.extend(_sync_failures)

    return failures


def drain_pending_signals():
    """Process at most one queued signal. Called between the loop's naps.

    One per pass on purpose: each signal can place orders, and doing them
    back-to-back inside one iteration would make a burst of stale alerts fire
    a burst of real trades with no gap to notice.

    NEVER RETRIES. Both stages above place orders, so re-running a partially
    successful signal could double a live position. A signal is marked done
    whether it succeeded or not; failures raise a Telegram alert instead."""
    with automation_lock:
        pend = kv_get(PENDING_SIGNALS_KEY, []) or []
        if not pend:
            return 0
        item = pend.pop(0)
        kv_set(PENDING_SIGNALS_KEY, pend)
        done = kv_get(PROCESSED_SIGNALS_KEY, []) or []
        done.append(item.get("id"))
        kv_set(PROCESSED_SIGNALS_KEY, done[-PROCESSED_HISTORY:])

    log_automation("Signal DEQUEUED %s @ %s (queued %s)"
                   % (item.get("signal"), item.get("spot"), item.get("queued_at")),
                   level="INFO")
    try:
        failures = _execute_signal(item)
    except Exception as e:
        failures = ["unexpected: %s" % e]
        log_automation("Signal execution crashed: %s" % e, level="ERROR")

    if failures:
        msg = ("\U0001F534 <b>SIGNAL FAILED</b>\n%s @ %s\n\n%s\n\n"
               "Not retried — a retry could double a live position. "
               "Check Kite and act manually."
               % (item.get("signal"), item.get("spot"), "\n".join(failures)))
        try:
            send_telegram(msg)
        except Exception:
            pass
        log_automation("SIGNAL FAILED %s: %s" % (item.get("id"), failures),
                       level="ERROR")
    return 1


_sync_done = {}

def position_sync_job():
    """Run position sync every 5 minutes during market hours."""
    while True:
        try:
            now = datetime.now(APP_TZ)
            hhmm = now.hour * 100 + now.minute
            # Only during market hours 9:15 AM - 3:30 PM on weekdays
            if now.weekday() < 5 and 915 <= hhmm <= 1530:
                cfg = get_automation_config()
                if cfg.get("mode") == "LIVE" and not automation_state.get("kill_switch"):
                    sync_positions_with_zerodha()
        except Exception as e:
            log_automation(f"Position sync job error: {e}", level="ERROR")
        # Legacy OB/AIT expiry rollover removed 2026-07-24 (models deleted).
        # Workstation rollover is handled by models_v2.scheduler_tick below.
        now_ist = datetime.now(APP_TZ)

        # ── Legacy NiftyEXP Mon/Tue scheduler retired 2026-07-24 (model removed).
        #    NiftyEXP Workstation is handled by models_v2.scheduler_tick below. ──

        try:
            check_users_missing_zerodha_before_open()
        except Exception as _zce:
            log_automation(f"Zerodha pre-open check ERROR: {_zce}", level="ERROR")

        try:
            check_kite_token_health()
        except Exception as _kth:
            log_automation(f"Kite token health ERROR: {_kth}", level="ERROR")

        # MODELS_V2 scheduler tick — Monday entry, Tuesday exit, Tuesday rollover.
        # Single-tenant: one global run. Multi-tenant: per-user, scoped.
        try:
            import sys as _sys
            _selfmod = _sys.modules[__name__]
            if multi_tenant_enabled():
                for _uid in active_trading_users():
                    try:
                        models_v2.scheduler_tick(_UserScopedApp(_selfmod, _uid), now_ist)
                    except Exception as _ue:
                        log_automation(f"scheduler_tick ERROR for user {_uid}: {_ue}", level="ERROR")
            else:
                models_v2.scheduler_tick(_selfmod, now_ist)
        except Exception as _mvse:
            log_automation(f"models_v2.scheduler_tick ERROR: {_mvse}", level="ERROR")

        # ────────────────────────────────────────────────────────────
        # One pass of this loop still takes ~60s, so the schedulers above keep
        # their cadence — models_v2.scheduler_tick is NOT safe to run twelve
        # times more often. The nap is split so a queued signal waits at most
        # ~5s instead of up to a minute.
        for _ in range(12):
            try:
                drain_pending_signals()
            except Exception as _dpe:
                log_automation(f"drain_pending_signals ERROR: {_dpe}", level="ERROR")
            time.sleep(5)


def ensure_sync_thread():
    if getattr(ensure_sync_thread, "_started", False):
        return
    t = threading.Thread(target=position_sync_job, name="stairs-sync", daemon=True)
    t.start()
    ensure_sync_thread._started = True

ensure_sync_thread()


@app.route("/api/automation/sync_positions", methods=["POST"])
def api_sync_positions():
    """Manually trigger position sync with Zerodha."""
    try:
        result = sync_positions_with_zerodha()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500




# ═══════════════════════════════════════════════════════════════════════════════
# WORKSTATION WEBHOOK — alias of the common SuperTrend ATR 10 / Mult 3.0 webhook
# Prefer ONE TradingView alert → /api/tradingview/webhook (Futures + OB + AIT).
# This URL stays for existing TV alerts; it enqueues the same unified signal queue.
# Do not keep two TV alerts on both URLs — use one to avoid duplicate flips.
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/tradingview/workstation_webhook", methods=["POST"])
def api_tradingview_workstation_webhook():
    """Alias of /api/tradingview/webhook — same SuperTrend ATR 10 / Mult 3.0 queue.

    Enqueues Futures + OB + AIT together (identical to the common webhook).
    """
    try:
        refresh_master_state_from_db()
        payload = parse_tradingview_payload()
        secret = str(payload.get("secret") or payload.get("token") or "").strip()
        if TRADINGVIEW_WEBHOOK_SECRET and secret != TRADINGVIEW_WEBHOOK_SECRET:
            log_automation("Rejected workstation webhook — invalid secret",
                           level="WARNING",
                           details={"remote_addr": request.remote_addr})
            return jsonify({"ok": False, "error": "invalid_secret"}), 403

        signal = normalize_signal(payload.get("signal") or payload.get("trend") or payload.get("side"))
        spot_raw = payload.get("close", payload.get("spot", payload.get("price")))
        if spot_raw is None:
            return jsonify({"ok": False, "error": "missing_close"}), 400
        spot = float(spot_raw)
        bar_time = payload.get("time_close") or payload.get("bar_time") or payload.get("time") or now_utc_iso()

        persist_master_state_updates({
            "nifty_spot": spot,
            "nifty_trend": signal,
            "nifty_lot_size": CURRENT_NIFTY_LOT_SIZE,
            "signal_source": "TRADINGVIEW_ST_10_3",
            "signal_time": str(bar_time),
            "nifty_trade_date": date.today().isoformat(),
        })
        refresh_master_state_from_db()

        log_automation(
            f"Common-alias signal {signal} @ {spot} ({models_v2.SIGNAL_SUPERTREND_LABEL})",
            details={"source": "TRADINGVIEW_ST_10_3", "bar_time": bar_time, "via": "workstation_webhook"}
        )

        ensure_sync_thread()
        _q = enqueue_signal(signal, spot, str(bar_time))
        return jsonify({
            "ok": True,
            "signal": signal,
            "spot": spot,
            "channel": "common_st_10_3",
            "queued": _q.get("queued"),
            "duplicate": _q.get("duplicate"),
            "signal_id": _q.get("id"),
            "supertrend": models_v2.SIGNAL_SUPERTREND_LABEL,
        })
    except Exception as e:
        log_automation(f"Workstation webhook error: {e}", level="ERROR",
                       details={"traceback": traceback.format_exc()})
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/ob_workstation", methods=["GET", "POST"])
def api_ob_workstation():
    """Return OB Workstation trades + config (separate from live OB)."""
    try:
        _bk = read_bundle_key("ob_workstation")
        data = kv_get(_bk, {}) or {}
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            new_config = body.get("config") if isinstance(body, dict) else None
            if new_config and new_config != data.get("config"):
                data["config"] = new_config
                kv_set(_bk, data)
        trades = data.get("trades", [])
        return jsonify({"trades": trades, "config": data.get("config", {}), "summary": {},
                        "archive": paper_phase_archive(data)})
    except Exception as e:
        return jsonify({"trades": [], "config": {}, "summary": {}, "error": str(e)}), 500


@app.route("/api/ait_workstation", methods=["GET", "POST"])
def api_ait_workstation():
    """Return AIT Workstation trades + config (separate from live AIT)."""
    try:
        _bk = read_bundle_key("ait_workstation")
        data = kv_get(_bk, {}) or {}
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            new_config = body.get("config") if isinstance(body, dict) else None
            if new_config and new_config != data.get("config"):
                data["config"] = new_config
                kv_set(_bk, data)
        trades = data.get("trades", [])
        return jsonify({"trades": trades, "config": data.get("config", {}), "summary": {},
                        "archive": paper_phase_archive(data)})
    except Exception as e:
        return jsonify({"trades": [], "config": {}, "summary": {}, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
