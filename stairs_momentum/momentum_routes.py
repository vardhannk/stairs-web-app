"""
momentum_routes.py
==================
Read-only Flask blueprint for the STAIRS UI. It ONLY reads the SQLite state
the job wrote — it never computes signals or touches Kite in a web request
(so Gunicorn's multiple workers can't double-fire anything).

Wire into your existing app.py with two lines:

    from momentum_routes import momentum_bp
    app.register_blueprint(momentum_bp)

Then protect it with your existing Google-OAuth `login_required` decorator if
you want it gated (see note below).
"""
from __future__ import annotations
from flask import Blueprint, jsonify, render_template_string
import stairs_store as store

momentum_bp = Blueprint("momentum", __name__, url_prefix="/momentum")


# If you have a login decorator in app.py, import and apply it instead.
def _maybe_login_required(f):
    try:
        from app import login_required          # your existing decorator
        return login_required(f)
    except Exception:                            # noqa
        return f


@momentum_bp.route("/api/latest")
@_maybe_login_required
def api_latest():
    return jsonify({
        "latest": store.kv_get(store.LATEST_KEY),
        "status": store.kv_get(store.STATUS_KEY),
    })


@momentum_bp.route("/api/history")
@_maybe_login_required
def api_history():
    return jsonify(store.kv_get(store.HISTORY_KEY) or [])


_PAGE = """
<!doctype html><meta charset=utf-8>
<title>Momentum • STAIRS</title>
<style>
 body{font:14px/1.5 system-ui;margin:24px;color:#1a1a1a;max-width:760px}
 h1{font-size:20px} .pill{padding:2px 8px;border-radius:10px;font-size:12px}
 .on{background:#e6f4ea;color:#137333}.off{background:#fce8e6;color:#c5221f}
 table{border-collapse:collapse;width:100%;margin-top:12px}
 td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}
 .muted{color:#777;font-size:12px}
</style>
<h1>Momentum Portfolio</h1>
<div id=root class=muted>Loading…</div>
<script>
fetch('/momentum/api/latest').then(r=>r.json()).then(d=>{
  const l=d.latest, s=d.status, root=document.getElementById('root');
  if(!l){root.textContent='No run yet.';return;}
  const inv=l.regime_invested;
  let rows=Object.entries(l.target||{}).sort((a,b)=>b[1]-a[1])
     .map(([k,v])=>`<tr><td>${k}</td><td>${(v*100).toFixed(1)}%</td></tr>`).join('');
  root.innerHTML=`
   <p>As of <b>${l.date}</b> &nbsp;
     <span class="pill ${inv?'on':'off'}">${inv?'INVESTED':'CASH (filter off)'}</span></p>
   <table><tr><th>Symbol</th><th>Weight</th></tr>${rows}
     <tr><td><i>CASH</i></td><td>${(l.cash*100).toFixed(1)}%</td></tr></table>
   <p class=muted>Generated ${l.generated_at} •
     last job ${s?s.last_run:'?'} • ${s&&s.ok?'ok':'ERROR'}</p>`;
});
</script>
"""


@momentum_bp.route("/")
@_maybe_login_required
def dashboard():
    return render_template_string(_PAGE)
