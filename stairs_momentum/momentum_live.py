"""
momentum_live.py
================
Live signal layer. Reuses the SAME engine modules the backtest uses, so the
strategy you trade is identical to the strategy you tested — no second
implementation to drift out of sync.

compute_target_for_date(): the rebalance logic for ONE date (today).
orders_from_target():      convert target weights -> concrete CNC orders.
"""
from __future__ import annotations
import pandas as pd
import datetime as dt

# engine package must be importable (sits alongside this folder)
from momentum.config import Config
from momentum.data import MarketData, make_rebalance_dates, eligibility_mask
from momentum.filters import market_ok, stock_filter_mask
from momentum.signals import momentum_score
from momentum.construction import build_target


def latest_trading_date(md: MarketData) -> pd.Timestamp:
    return md.prices.index[-1]


def is_rebalance_today(md: MarketData, cfg: Config,
                       on: pd.Timestamp | None = None) -> bool:
    on = on or latest_trading_date(md)
    return on in set(make_rebalance_dates(md.prices.index, cfg))


def compute_target_for_date(md: MarketData, cfg: Config,
                            date: pd.Timestamp | None = None):
    """
    Returns (target_weights: pd.Series, regime_ok: bool). Mirrors exactly what
    the backtest engine does on a rebalance date.
    """
    date = date or latest_trading_date(md)
    prices = md.prices.ffill()
    regime = market_ok(md.market_index, date, cfg)
    if not regime:
        return pd.Series(dtype=float), False
    elig = eligibility_mask(md, date, cfg)
    trend = stock_filter_mask(prices, date, cfg, elig)
    scored = momentum_score(prices, date, cfg, trend)
    target = build_target(prices, date, scored, md.sectors, cfg)
    return target, True


def orders_from_target(target: pd.Series, current_holdings: dict,
                       portfolio_value: float, prices_now: dict,
                       min_order_value: float = 1000.0):
    """
    Translate target weights -> list of order dicts (BUY/SELL, qty).
    current_holdings: {symbol: qty} from kite.holdings()
    prices_now:       {symbol: ltp}
    Returns orders you can review before sending. NOTHING is placed here.
    """
    orders = []
    target = target[target > 0]
    universe = set(target.index) | set(current_holdings)
    for sym in sorted(universe):
        ltp = prices_now.get(sym)
        if not ltp or ltp <= 0:
            continue
        tgt_val = float(target.get(sym, 0.0)) * portfolio_value
        tgt_qty = int(tgt_val // ltp)
        cur_qty = int(current_holdings.get(sym, 0))
        delta = tgt_qty - cur_qty
        if abs(delta) * ltp < min_order_value:
            continue
        orders.append({
            "symbol": sym,
            "side": "BUY" if delta > 0 else "SELL",
            "qty": abs(delta),
            "ltp": ltp,
            "approx_value": round(abs(delta) * ltp, 2),
            "target_weight": round(float(target.get(sym, 0.0)), 4),
        })
    return orders
