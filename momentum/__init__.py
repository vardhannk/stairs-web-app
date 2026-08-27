"""
momentum
========
A rules-based momentum portfolio engine for Indian equities (BSE 500 /
Nifty 500), with pluggable momentum models, trend filters, weighting
schemes, risk controls, backtesting, reporting and optimisation.

Quick start
-----------
    from momentum import Config, MarketData, run_backtest
    from momentum import reporting

    md  = MarketData(prices, volume, sectors, benchmarks, market_index)
    cfg = Config(universe="bse500", model="m3", top_n=20,
                 weighting="equal", rebalance="monthly")
    res = run_backtest(md, cfg)
    print(reporting.performance_table(res, benchmarks))
"""
from .config import Config, TRADING_DAYS_YEAR, TRADING_DAYS_MONTH
from .data import MarketData, make_rebalance_dates, eligibility_mask
from .engine import run_backtest, BacktestResult
from . import signals, filters, construction, metrics, reporting, optimize

__all__ = [
    "Config", "MarketData", "run_backtest", "BacktestResult",
    "make_rebalance_dates", "eligibility_mask",
    "signals", "filters", "construction", "metrics", "reporting", "optimize",
    "TRADING_DAYS_YEAR", "TRADING_DAYS_MONTH",
]
