"""
filters.py
==========
Trend filters from the brief.

Market filter : invest only when the broad index is above its MA.
Stock filter  : invest only in names above 200DMA & 50DMA and making
                higher-highs / higher-lows.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .config import Config


def market_ok(market_index: pd.Series, date: pd.Timestamp, cfg: Config) -> bool:
    """True if invested regime (index above its MA), or filter disabled."""
    if not cfg.use_market_filter:
        return True
    loc = market_index.index.get_loc(date)
    loc = loc.start if isinstance(loc, slice) else loc
    if loc < cfg.market_ma_days:
        return False
    ma = market_index.iloc[loc - cfg.market_ma_days:loc].mean()
    return bool(market_index.iloc[loc] > ma)


def _higher_highs_lows(px_col: pd.Series, loc: int, cfg: Config) -> bool:
    """
    Confirm an uptrend structure: split the recent lookback into two halves
    and require the later half's swing high AND swing low to exceed the
    earlier half's. A simple, robust HH/HL proxy.
    """
    lb = cfg.hh_hl_lookback
    if loc < lb:
        return False
    win = px_col.iloc[loc - lb:loc].dropna()
    if len(win) < cfg.hh_hl_window * 2:
        return False
    half = len(win) // 2
    early, late = win.iloc[:half], win.iloc[half:]
    return (late.max() > early.max()) and (late.min() > early.min())


def stock_filter_mask(prices: pd.DataFrame, date: pd.Timestamp,
                      cfg: Config, eligible: pd.Series) -> pd.Series:
    """
    Boolean Series over tickers passing the trend filter. Vectorised for the
    MA tests; HH/HL is done per-eligible-column (cheap, only on candidates).
    """
    if not cfg.use_stock_filter:
        return eligible.copy()

    loc = prices.index.get_loc(date)
    loc = loc.start if isinstance(loc, slice) else loc
    cur = prices.iloc[loc]

    if loc < cfg.stock_ma_long:
        return pd.Series(False, index=prices.columns)

    ma200 = prices.iloc[loc - cfg.stock_ma_long:loc].mean()
    ma50 = prices.iloc[loc - cfg.stock_ma_short:loc].mean()
    above = (cur > ma200) & (cur > ma50)

    mask = above & eligible
    if cfg.require_hh_hl:
        cands = mask[mask].index
        hhhl = pd.Series(False, index=prices.columns)
        lb = cfg.hh_hl_lookback
        if loc >= lb and len(cands):
            win = prices[cands].iloc[loc - lb:loc]      # (lb x n_cand)
            half = len(win) // 2
            early, late = win.iloc[:half], win.iloc[half:]
            ok = (late.max() > early.max()) & (late.min() > early.min())
            # require enough valid observations
            ok &= win.notna().sum() >= cfg.hh_hl_window * 2
            hhhl.loc[cands] = ok.reindex(cands).fillna(False)
        mask = mask & hhhl
    return mask.fillna(False)
