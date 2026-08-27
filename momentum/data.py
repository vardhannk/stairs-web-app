"""
data.py
=======
Data handling for the momentum engine.

This module is deliberately source-agnostic. You bring *already adjusted*
daily closes (split / bonus / dividend / corporate-action adjusted) plus
daily traded value, and it produces the clean panels + eligibility masks
the engine needs.

Expected inputs
---------------
prices  : DataFrame  index=DatetimeIndex (daily), columns=tickers, adjusted close
volume  : DataFrame  same shape, traded *value* in INR (price*qty). Optional.
sectors : dict       ticker -> sector string
benchmarks : dict    name -> Series of TRI levels (e.g. {"NIFTY50_TRI": ...})

In production, adjustment is done by your data vendor / pipeline (e.g.
nsepy, jugaad-data, Norgate, or a paid feed). See deployment roadmap.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass
from .config import Config


@dataclass
class MarketData:
    prices: pd.DataFrame                 # adjusted close
    volume: pd.DataFrame | None          # traded value (INR)
    sectors: dict                        # ticker -> sector
    benchmarks: dict                     # name -> Series (TRI level)
    market_index: pd.Series              # index used for the market filter (e.g. Nifty 50 price)

    def __post_init__(self):
        # align everything to the price calendar, forward-fill small gaps
        self.prices = self.prices.sort_index()
        cal = self.prices.index
        if self.volume is not None:
            self.volume = self.volume.reindex(cal)
        self.market_index = self.market_index.reindex(cal).ffill()
        for k in self.benchmarks:
            self.benchmarks[k] = self.benchmarks[k].reindex(cal).ffill()

    @property
    def tickers(self) -> list:
        return list(self.prices.columns)


def make_rebalance_dates(index: pd.DatetimeIndex, cfg: Config) -> pd.DatetimeIndex:
    """First trading day of each week or month within the price calendar."""
    s = pd.Series(index, index=index)
    if cfg.rebalance == "weekly":
        # one rebalance per ISO week -> take the first session of each week
        grp = s.groupby([index.isocalendar().year, index.isocalendar().week])
    elif cfg.rebalance == "monthly":
        grp = s.groupby([index.year, index.month])
    else:
        raise ValueError(cfg.rebalance)
    firsts = grp.first()
    return pd.DatetimeIndex(sorted(firsts.values))


def eligibility_mask(md: MarketData, date: pd.Timestamp, cfg: Config) -> pd.Series:
    """
    Boolean Series over tickers: True == eligible to be *considered* on `date`.
    Enforces: >=1y history, ADV floor, illiquidity-gap ceiling.
    """
    px = md.prices
    loc = px.index.get_loc(date)
    if isinstance(loc, slice):
        loc = loc.start
    tickers = px.columns

    # --- min history: a valid price min_history_days ago and today
    if loc < cfg.min_history_days:
        return pd.Series(False, index=tickers)
    hist_ok = px.iloc[loc - cfg.min_history_days].notna() & px.iloc[loc].notna()

    # --- ADV floor over recent window
    if md.volume is not None:
        start = max(0, loc - cfg.adv_lookback_days)
        adv = md.volume.iloc[start:loc].mean()
        adv_ok = adv >= cfg.min_adv_inr
    else:
        adv_ok = pd.Series(True, index=tickers)

    # --- illiquidity: too many missing / zero days in the recent window
    start = max(0, loc - cfg.adv_lookback_days)
    win_px = px.iloc[start:loc]
    nan_frac = win_px.isna().mean()
    if md.volume is not None:
        zero_frac = (md.volume.iloc[start:loc].fillna(0) <= 0).mean()
        gap_frac = nan_frac + zero_frac
    else:
        gap_frac = nan_frac
    liq_ok = gap_frac <= cfg.max_illiquid_gap_frac

    return (hist_ok & adv_ok & liq_ok).fillna(False)
