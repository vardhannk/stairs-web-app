# Deploying the Momentum module into STAIRS

Target stack: `/opt/stairs-web-app/` · Flask `app.py` · Gunicorn systemd
`stairs-web-app` · Nginx · SQLite `stairs_state.db` (`kv` + `automation_log`).

Design in one line: **the signal job runs as a systemd timer (one process),
the Flask side only reads SQLite.** No in-process scheduler inside Gunicorn —
that is what prevents the multi-worker double-fire you hit with the rollover
scheduler.

```
/opt/stairs-web-app/
├── app.py                      # your existing app (add 2 lines)
├── stairs_state.db             # existing SQLite
├── momentum/                   # ENGINE package (copy from momentum_india/)
├── momentum_universe.csv       # you create: columns  symbol,sector
└── stairs_momentum/            # this integration folder
    ├── stairs_store.py
    ├── momentum_live.py
    ├── kite_data.py
    ├── momentum_job.py         # run by the timer
    ├── momentum_routes.py      # Flask blueprint (read-only)
    └── deploy/
        ├── stairs-momentum.service
        └── stairs-momentum.timer
```

## 1. Copy files up
From your machine:
```bash
scp -r momentum_india/momentum  root@152.42.171.198:/opt/stairs-web-app/
scp -r stairs_momentum          root@152.42.171.198:/opt/stairs-web-app/
```

## 2. Dependencies (into the existing venv)
```bash
/opt/stairs-web-app/.venv/bin/pip install kiteconnect pyarrow
# pandas/numpy/matplotlib already present from STAIRS
```

## 3. Build the universe file
`momentum_universe.csv` = the names you want the strategy to choose from.
For LIVE trading, today's Nifty 500 / BSE 500 constituents are correct
(survivorship only matters for backtests). Minimal example:
```
symbol,sector
RELIANCE,Energy
INFY,IT
TCS,IT
HDFCBANK,Financials
ICICIBANK,Financials
ITC,Consumer
LT,Industrials
SUNPHARMA,Pharma
MARUTI,Auto
TATASTEEL,Metals
...
```
Put it at `/opt/stairs-web-app/momentum_universe.csv`.

## 4. Kite access token (the main operational gotcha)
Kite tokens expire daily. `kite_data.get_kite()` reads, in order:
`KITE_ACCESS_TOKEN` env → `/opt/stairs-web-app/.kite_token`.
Easiest: after your daily Kite login (account DN3823), write the token:
```bash
echo "PASTE_TODAYS_ACCESS_TOKEN" > /opt/stairs-web-app/.kite_token
chmod 600 /opt/stairs-web-app/.kite_token
```
Better: have whatever STAIRS already uses to refresh its Kite session also
write this file (or edit `get_kite()` to read your existing token store).

## 5. Wire the dashboard into app.py (2 lines)
```python
from stairs_momentum.momentum_routes import momentum_bp
app.register_blueprint(momentum_bp)
```
The blueprint auto-applies your `login_required` decorator if it can import it
from `app`. Nginx already proxies Flask, so `https://stairstomillionaire.com/momentum`
works once Gunicorn restarts. Then:
```bash
systemctl restart stairs-web-app
```

## 6. Install the timer (runs the job after close, Mon–Fri)
```bash
cp /opt/stairs-web-app/stairs_momentum/deploy/stairs-momentum.* /etc/systemd/system/
# IMPORTANT: check server timezone. If it's UTC, edit the .timer OnCalendar
# from "16:15" (IST) to "10:45" (UTC). Confirm with:  timedatectl
systemctl daemon-reload
systemctl enable --now stairs-momentum.timer
systemctl list-timers | grep momentum     # verify next run
```

## 7. First manual run (use the /tmp heredoc pattern you already use)
```bash
cd /opt/stairs-web-app/stairs_momentum
STAIRS_DB_PATH=/opt/stairs-web-app/stairs_state.db \
  /opt/stairs-web-app/.venv/bin/python momentum_job.py
```
Expected: it prints the target basket, writes `momentum:latest` into `kv`, and
appends a `momentum:run` row to `automation_log`. Open `/momentum` to see it.

## 8. Telegram alerts (optional)
Set `TELEGRAM_BOT_TOKEN` in the service `Environment=` (chat id 6119732697 is
already wired). You'll get an alert on rebalance days with the new basket.

## 9. Order execution — deliberately manual
`momentum_job.py` never sends orders. With `MOMENTUM_LIVE=1` it only PRINTS
proposed CNC orders (BUY/SELL + qty) and stores them under
`momentum:proposed_orders` for review. To go fully automated you implement
placement yourself behind your own approval gate (a `kite.place_order(...)`
loop). An EOD basket is slow-moving — give it a human glance before firing,
and keep it well away from the OB/AIT webhook path so the two can't interfere.

---

### Safety / correctness checklist before you trust it
- [ ] The `get_config()` in `momentum_job.py` matches a config you actually
      backtested on a **survivorship-bias-free** universe (not the synthetic demo).
- [ ] `momentum_universe.csv` is real current constituents.
- [ ] Server timezone vs the timer's `OnCalendar` is correct.
- [ ] `.kite_token` is refreshed daily (else the job errors + Telegram-alerts you).
- [ ] You've watched `MOMENTUM_LIVE=1` proposals for a few rebalances before
      automating placement.
- [ ] Whatever you show users is honest, cost-realistic, and risk-disclosed.
```
