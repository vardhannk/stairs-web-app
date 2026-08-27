"""
signals.py
==========
The three momentum models from the brief, returning a per-ticker score
(higher = stronger momentum) computed *as of* a rebalance date using only
information available up to that date (no look-ahead).

Model 1  m1 : classic 12-1 total return
Model 2  m2 : dual momentum (absolute gate vs RF + relative rank)
Model 3  m3 : multi-factor composite (3m/6m/12m return, RS rank, dist-52w-high)
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .config import Config


def _ret(prices: pd.DataFrame, loc: int, lookback: int, skip: int = 0) -> pd.Series:
    """Total return from (loc-lookback) to (loc-skip)."""
    a = loc - lookback
    b = loc - skip
    if a < 0:
        return pd.Series(np.nan, index=prices.columns)
    p0 = prices.iloc[a]
    p1 = prices.iloc[b - 1] if skip > 0 else prices.iloc[b]
    return p1 / p0 - 1.0


def _loc(prices: pd.DataFrame, date: pd.Timestamp) -> int:
    loc = prices.index.get_loc(date)
    return loc.start if isinstance(loc, slice) else loc


def model1_12_1(prices: pd.DataFrame, date: pd.Timestamp, cfg: Config) -> pd.Series:
    loc = _loc(prices, date)
    return _ret(prices, loc, cfg.lookback_long_days, cfg.lookback_skip_days)


def model2_dual(prices: pd.DataFrame, date: pd.Timestamp, cfg: Config) -> pd.DataFrame:
    """
    Returns a frame with the relative score plus the absolute-momentum gate.
    'score' is the trailing 12-1 return; 'abs_ok' is the absolute filter
    (return must beat the risk-free rate over the same horizon).
    """
    loc = _loc(prices, date)
    r = _ret(prices, loc, cfg.lookback_long_days, cfg.lookback_skip_days)
    horizon = (cfg.lookback_long_days - cfg.lookback_skip_days) / 252.0
    rf_hurdle = (1 + cfg.risk_free_annual) ** horizon - 1
    out = pd.DataFrame({"score": r})
    out["abs_ok"] = r > rf_hurdle
    return out


def _dist_from_52w_high(prices: pd.DataFrame, loc: int, cfg: Config) -> pd.Series:
    a = max(0, loc - cfg.high_lookback_days)
    window = prices.iloc[a:loc]
    hi = window.max()
    cur = prices.iloc[loc - 1]
    # closeness to high: 0 == at the high, negative == below. Higher is better.
    return (cur / hi) - 1.0


def model3_composite(prices: pd.DataFrame, date: pd.Timestamp, cfg: Config,
                     eligible: pd.Series) -> pd.Series:
    """
    Cross-sectional composite. Each component is converted to a percentile
    rank across the *eligible* set, then weighted and averaged.
    """
    loc = _loc(prices, date)
    r3 = _ret(prices, loc, cfg.lookback_short_days, cfg.lookback_skip_days)
    r6 = _ret(prices, loc, cfg.lookback_mid_days, cfg.lookback_skip_days)
    r12 = _ret(prices, loc, cfg.lookback_long_days, cfg.lookback_skip_days)
    rs = r12  # relative-strength proxy = 12-1 return, ranked below
    dist = _dist_from_52w_high(prices, loc, cfg)

    comp = pd.DataFrame({"r3": r3, "r6": r6, "r12": r12, "rs": rs, "dist": dist})
    comp = comp[eligible.reindex(comp.index).fillna(False)]

    # percentile rank each column (NaNs drop out of the ranking)
    ranks = comp.rank(pct=True)
    w = np.array([cfg.w_ret_3m, cfg.w_ret_6m, cfg.w_ret_12m,
                  cfg.w_rs_rank, cfg.w_dist_52w_high], dtype=float)
    w = w / w.sum()
    score = (ranks[["r3", "r6", "r12", "rs", "dist"]] * w).sum(axis=1, min_count=1)
    return score.reindex(prices.columns)


def momentum_score(prices: pd.DataFrame, date: pd.Timestamp, cfg: Config,
                   eligible: pd.Series) -> pd.DataFrame:
    """
    Unified entry point. Returns frame indexed by ticker with columns:
        score   : float momentum score (higher better)
        abs_ok  : bool  absolute-momentum gate (always True unless model 2)
    Only eligible tickers carry a non-NaN score.
    """
    if cfg.model == "m1":
        s = model1_12_1(prices, date, cfg)
        out = pd.DataFrame({"score": s})
        out["abs_ok"] = True
    elif cfg.model == "m2":
        out = model2_dual(prices, date, cfg)
    elif cfg.model == "m3":
        s = model3_composite(prices, date, cfg, eligible)
        out = pd.DataFrame({"score": s})
        out["abs_ok"] = True
    else:
        raise ValueError(cfg.model)

    out.loc[~eligible.reindex(out.index).fillna(False), "score"] = np.nan
    out["abs_ok"] = out["abs_ok"].fillna(False)
    return out
