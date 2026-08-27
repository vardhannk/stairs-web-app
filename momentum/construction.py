"""
construction.py
===============
Turn a ranked set of qualifying stocks into a target weight vector.

Weighting schemes : equal | rank | vol (inverse-volatility)
Constraints       : max position cap, sector cap, gross-exposure / vol target
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .config import Config, TRADING_DAYS_YEAR


def _trailing_vol(prices: pd.DataFrame, date: pd.Timestamp, tickers, lookback: int) -> pd.Series:
    loc = prices.index.get_loc(date)
    loc = loc.start if isinstance(loc, slice) else loc
    sub = prices[tickers].iloc[max(0, loc - lookback):loc]
    rets = sub.pct_change()
    return rets.std() * np.sqrt(TRADING_DAYS_YEAR)


def raw_weights(prices: pd.DataFrame, date: pd.Timestamp, picks: pd.Series,
                cfg: Config) -> pd.Series:
    """
    picks : Series indexed by selected tickers, values = momentum score.
    Returns un-capped weights summing to 1.
    """
    tickers = list(picks.index)
    if cfg.weighting == "equal":
        w = pd.Series(1.0 / len(tickers), index=tickers)
    elif cfg.weighting == "rank":
        # weight proportional to rank position (strongest gets most)
        order = picks.rank(ascending=True)  # 1..N, N strongest
        w = order / order.sum()
    elif cfg.weighting == "vol":
        vol = _trailing_vol(prices, date, tickers, cfg.vol_lookback_days)
        inv = 1.0 / vol.replace(0, np.nan)
        inv = inv.fillna(inv.median())
        w = inv / inv.sum()
    else:
        raise ValueError(cfg.weighting)
    return w


def apply_caps(weights: pd.Series, sectors: dict, cfg: Config,
               iters: int = 50) -> pd.Series:
    """
    Iteratively enforce per-name and per-sector caps, redistributing the
    excess to the unconstrained names. Converges quickly in practice.
    """
    w = weights.copy()
    sec = pd.Series({t: sectors.get(t, "UNKNOWN") for t in w.index})
    for _ in range(iters):
        changed = False
        # name cap
        over = w > cfg.max_position_weight + 1e-12
        if over.any():
            excess = (w[over] - cfg.max_position_weight).sum()
            w[over] = cfg.max_position_weight
            free = ~over
            if free.any() and excess > 0:
                w[free] += excess * w[free] / w[free].sum()
                changed = True
        # sector cap
        sec_tot = w.groupby(sec).sum()
        breached = sec_tot[sec_tot > cfg.sector_cap + 1e-12]
        for s, tot in breached.items():
            members = sec[sec == s].index
            scale = cfg.sector_cap / tot
            excess = (w[members].sum()) - cfg.sector_cap
            w[members] *= scale
            free = sec[sec != s].index
            free = [t for t in free if w[t] < cfg.max_position_weight - 1e-9]
            if free and excess > 0:
                w.loc[free] += excess * w[free] / w[free].sum()
                changed = True
        if not changed:
            break
    # numerical clean-up
    w = w.clip(lower=0)
    if w.sum() > 0:
        w = w / w.sum()
    return w


def vol_target_scale(prices: pd.DataFrame, date: pd.Timestamp, weights: pd.Series,
                     cfg: Config) -> float:
    """
    Scale gross exposure so realised portfolio vol ~ target. Returns a
    multiplier in [0, max_leverage]; remainder is held in cash.
    """
    if cfg.target_vol_annual is None or weights.empty:
        return min(1.0, cfg.max_leverage)
    loc = prices.index.get_loc(date)
    loc = loc.start if isinstance(loc, slice) else loc
    sub = prices[list(weights.index)].iloc[max(0, loc - cfg.target_vol_lookback):loc]
    rets = sub.pct_change().dropna(how="all")
    if len(rets) < 5:
        return min(1.0, cfg.max_leverage)
    cov = rets.cov() * TRADING_DAYS_YEAR
    w = weights.values
    port_vol = float(np.sqrt(max(w @ cov.values @ w, 1e-12)))
    scale = cfg.target_vol_annual / port_vol if port_vol > 0 else 1.0
    return float(np.clip(scale, 0.0, cfg.max_leverage))


def build_target(prices: pd.DataFrame, date: pd.Timestamp, scored: pd.DataFrame,
                 sectors: dict, cfg: Config) -> pd.Series:
    """
    Full construction pipeline -> target weight Series (may sum to < 1 if
    vol-targeting de-risks into cash; the cash slug is implicit = 1 - sum).
    """
    cand = scored.dropna(subset=["score"])
    cand = cand[cand["abs_ok"]]
    if cand.empty:
        return pd.Series(dtype=float)
    picks = cand["score"].sort_values(ascending=False).head(cfg.top_n)
    w = raw_weights(prices, date, picks, cfg)
    w = apply_caps(w, sectors, cfg)
    scale = vol_target_scale(prices, date, w, cfg)
    return (w * scale).round(6)
