"""
momentum_job.py
===============
Standalone job — run by the systemd TIMER (NOT inside Gunicorn). This is the
deliberate design choice that avoids the multi-worker duplicate-execution
problem that bit the rollover scheduler: one process, one run, per schedule.

What it does each run (intended: daily after NSE close):
  1. Build live MarketData for the current universe.
  2. If today is a rebalance date -> compute the new target basket.
     Else -> just refresh the "current intended holdings" snapshot.
  3. Persist target + status to SQLite (kv/automation_log) for the dashboard.
  4. Telegram alert with the new basket.
  5. Order placement is OFF by default. With MOMENTUM_LIVE=1 it only PRINTS
     the orders for you to review — it still does not auto-send. Flip the
     final guard yourself once you've watched it for a while.

Usage:
    python momentum_job.py            # compute + store + alert (no orders)
    MOMENTUM_LIVE=1 python momentum_job.py   # also print proposed orders
"""
from __future__ import annotations
import os, sys, json, traceback
import datetime as dt
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/stairs-web-app")          # so `momentum` package resolves

from momentum.config import Config
import stairs_store as store
import momentum_live as live
import kite_data

UNIVERSE_CSV = "/opt/stairs-web-app/momentum_universe.csv"   # cols: symbol,sector
TELEGRAM_CHAT_ID = "6119732697"


# ---- strategy config (must match what you validated in backtest) -----------
def get_config() -> Config:
    return Config(
        model="m3", top_n=20, weighting="equal", rebalance="monthly",
        min_history_days=252, min_adv_inr=50_000_000,
        lookback_long_days=252, lookback_mid_days=126, lookback_short_days=63,
        lookback_skip_days=21, high_lookback_days=252,
        market_ma_days=50, stock_ma_long=200, stock_ma_short=50,
        require_hh_hl=True, max_position_weight=0.10, sector_cap=0.30,
        target_vol_annual=0.15, stop_type="pct", stop_pct=0.20,
        risk_free_annual=0.06, name="stairs_momentum_live",
    )


def load_universe():
    df = pd.read_csv(UNIVERSE_CSV)
    symbols = list(df["symbol"])
    sectors = dict(zip(df["symbol"], df.get("sector", ["UNKNOWN"] * len(df))))
    return symbols, sectors


def telegram_alert(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID, "text": text,
            "parse_mode": "Markdown"}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
    except Exception:                              # noqa
        pass


def format_basket(date, target: pd.Series, regime: bool) -> str:
    if not regime:
        return f"*Momentum* {date}\nMarket filter OFF → *100% CASH*."
    if target.empty:
        return f"*Momentum* {date}\nNo qualifying names → *CASH*."
    lines = [f"*Momentum target* {date}  (Nifty>50DMA: invested)"]
    for sym, wt in target.sort_values(ascending=False).items():
        lines.append(f"  {sym}: {wt:.1%}")
    lines.append(f"  CASH: {1 - target.sum():.1%}")
    return "\n".join(lines)


def main():
    cfg = get_config()
    try:
        symbols, sectors = load_universe()
        md = kite_data.build_live_market_data(symbols, sectors)
        date = live.latest_trading_date(md)
        rebal = live.is_rebalance_today(md, cfg, date)
        target, regime = live.compute_target_for_date(md, cfg, date)

        store.save_target(date, dict(target), regime,
                          equity_snap=None)
        store.log_event("run", f"date={date} rebalance={rebal} "
                               f"regime={regime} n={len(target)}")
        store.set_status(True, f"ok; rebalance={rebal}")

        msg = format_basket(str(date.date()), target, regime)
        if rebal:
            telegram_alert("🔔 REBALANCE DAY\n" + msg)
        print(msg)

        # ---- order proposal (review only; never auto-sent here) ------------
        if os.environ.get("MOMENTUM_LIVE") == "1" and regime and not target.empty:
            kite = kite_data.get_kite()
            holdings = {h["tradingsymbol"]: h["quantity"] for h in kite.holdings()}
            ltps = kite.ltp([f"NSE:{s}" for s in target.index])
            prices_now = {s.split(":")[1]: v["last_price"] for s, v in ltps.items()}
            pv = float(kite.margins()["equity"]["net"])     # or your tracked NAV
            orders = live.orders_from_target(target, holdings, pv, prices_now)
            print("\nPROPOSED ORDERS (NOT SENT — review then place yourself):")
            print(json.dumps(orders, indent=2))
            store.kv_set("momentum:proposed_orders",
                         {"date": str(date), "orders": orders})
            # To actually trade, YOU implement placement here behind your own
            # approval gate, e.g. kite.place_order(... ) per order. Left out on
            # purpose: an EOD basket should get a human glance before firing.

    except Exception as e:                          # noqa
        store.set_status(False, f"error: {e}")
        store.log_event("error", traceback.format_exc()[-1500:])
        telegram_alert(f"⚠️ Momentum job FAILED: {e}")
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
