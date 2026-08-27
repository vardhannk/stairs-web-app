"""
engine.py
=========
Event-driven daily backtest engine.

Flow per day:
  1. Mark-to-market held positions; accrue risk-free on cash.
  2. Update trailing peaks; trigger %/ATR stops -> move stopped names to cash.
On each rebalance date (after MTM):
  3. If market filter says "risk-off" -> target = all cash.
  4. Else compute eligibility, momentum scores, trend filter, build target.
  5. Trade from current -> target; charge cost on one-way turnover.

Outputs a BacktestResult with the equity curve, daily returns, holdings at
each rebalance, and a full trade log.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field

from .config import Config
from .data import MarketData, make_rebalance_dates, eligibility_mask
from .signals import momentum_score
from .filters import market_ok, stock_filter_mask
from .construction import build_target


@dataclass
class BacktestResult:
    cfg: Config
    equity: pd.Series
    daily_ret: pd.Series
    holdings_history: dict          # date -> target weight Series
    trade_log: pd.DataFrame
    exposure: pd.Series             # daily gross invested fraction
    regime: pd.Series               # daily market-filter on/off


def _atr_close(prices: pd.DataFrame, loc: int, period: int) -> pd.Series:
    """Close-based ATR proxy (mean abs daily change). Use true OHLC ATR live."""
    sub = prices.iloc[max(0, loc - period):loc]
    return sub.diff().abs().mean()


def run_backtest(md: MarketData, cfg: Config) -> BacktestResult:
    prices = md.prices.ffill()       # forward-fill for MTM continuity
    cal = prices.index
    rebal_dates = set(make_rebalance_dates(cal, cfg))

    cost_rate = (cfg.cost_bps + cfg.slippage_bps) / 1e4
    rf_d = cfg.rf_daily

    # state
    w = pd.Series(dtype=float)       # current weights (fraction of equity)
    cash = 1.0
    peak = pd.Series(dtype=float)    # peak price since entry, per held name
    entry = pd.Series(dtype=float)   # entry price, per held name

    equity = [1.0]
    dates_out = [cal[0]]
    daily_rets = [0.0]
    exposure = [0.0]
    regime_flags = [True]
    holdings_history: dict = {}
    trades: list = []

    prev_px = prices.iloc[0]

    for i in range(1, len(cal)):
        date = cal[i]
        px = prices.iloc[i]

        # ---- 1. mark to market -------------------------------------------
        if len(w):
            r = (px[w.index] / prev_px[w.index] - 1.0).fillna(0.0)
        else:
            r = pd.Series(dtype=float)
        port_ret = float((w * r).sum()) + cash * rf_d
        # drift weights then renormalise to fractions of new equity
        if len(w):
            w = w * (1 + r)
        cash = cash * (1 + rf_d)
        tot = float(w.sum()) + cash
        if tot > 0:
            w = w / tot
            cash = cash / tot

        # ---- 2. trailing stops -------------------------------------------
        if len(w) and cfg.stop_type != "none":
            held = w.index
            peak = peak.reindex(held).fillna(px[held])
            peak = np.maximum(peak, px[held])
            if cfg.stop_type == "pct":
                stop_level = peak * (1 - cfg.stop_pct)
            else:  # atr
                atr = _atr_close(prices, i, cfg.atr_period).reindex(held)
                stop_level = peak - cfg.atr_mult * atr
            hit = px[held] < stop_level
            for t in held[hit.values]:
                cash += w[t]
                trades.append(dict(date=date, ticker=t, side="SELL",
                                   weight=-float(w[t]), reason="stop",
                                   cost=float(w[t]) * cost_rate))
                cash -= float(w[t]) * cost_rate
            if hit.any():
                w = w[~hit.reindex(w.index).fillna(False).values]
                peak = peak.reindex(w.index)

        # ---- 3. rebalance -------------------------------------------------
        reg = market_ok(md.market_index, date, cfg)
        if date in rebal_dates:
            if not reg:
                target = pd.Series(dtype=float)          # go to cash
            else:
                elig = eligibility_mask(md, date, cfg)
                trend = stock_filter_mask(prices, date, cfg, elig)
                scored = momentum_score(prices, date, cfg, trend)
                target = build_target(prices, date, scored, md.sectors, cfg)

            # turnover & trades current(w) -> target
            idx = w.index.union(target.index)
            cur = w.reindex(idx, fill_value=0.0)
            tgt = target.reindex(idx, fill_value=0.0)
            delta = tgt - cur
            turn = delta.abs().sum() / 2.0
            cash -= turn * cost_rate
            for t in idx:
                d = float(delta[t])
                if abs(d) < 1e-6:
                    continue
                trades.append(dict(date=date, ticker=t,
                                   side="BUY" if d > 0 else "SELL",
                                   weight=d, reason="rebalance",
                                   cost=abs(d) * cost_rate))
            # set new state
            new_entries = target.index.difference(w.index)
            w = target.copy()
            cash = 1.0 - float(w.sum())
            # reset peaks: keep running peak for continuing names, init new ones
            peak = peak.reindex(w.index)
            peak.loc[new_entries] = px[new_entries]
            peak = peak.fillna(px.reindex(peak.index))
            entry = entry.reindex(w.index)
            entry.loc[new_entries] = px[new_entries]
            holdings_history[date] = w.copy()

        # ---- record ------------------------------------------------------
        equity.append(equity[-1] * (1 + port_ret))
        dates_out.append(date)
        daily_rets.append(port_ret)
        exposure.append(float(w.sum()))
        regime_flags.append(bool(reg))
        prev_px = px

    eq = pd.Series(equity, index=pd.DatetimeIndex(dates_out)) * cfg.initial_capital
    dr = pd.Series(daily_rets, index=pd.DatetimeIndex(dates_out))
    expo = pd.Series(exposure, index=pd.DatetimeIndex(dates_out))
    reg_s = pd.Series(regime_flags, index=pd.DatetimeIndex(dates_out))
    tl = pd.DataFrame(trades)
    if not tl.empty:
        tl = tl.sort_values("date").reset_index(drop=True)
    return BacktestResult(cfg, eq, dr, holdings_history, tl, expo, reg_s)
