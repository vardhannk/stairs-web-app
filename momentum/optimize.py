"""
optimize.py
===========
Grid-search over the configurations the brief asks to compare and rank by
Sharpe / Calmar / CAGR / MaxDD. Returns a tidy results frame plus the best
config under each objective.
"""
from __future__ import annotations
import itertools
import copy
import pandas as pd

from .config import Config
from .engine import run_backtest
from . import metrics as M


DEFAULT_GRID = {
    "top_n": [10, 15, 20, 25, 30],
    "rebalance": ["weekly", "monthly"],
    "weighting": ["equal", "vol"],
    "lookback_long_days": [126, 189, 252],   # 6m / 9m / 12m momentum lookbacks
}


def run_grid(md, base_cfg: Config, grid: dict | None = None,
             progress: bool = True) -> pd.DataFrame:
    grid = grid or DEFAULT_GRID
    keys = list(grid)
    combos = list(itertools.product(*[grid[k] for k in keys]))
    rows = []
    for n, combo in enumerate(combos, 1):
        cfg = copy.deepcopy(base_cfg)
        for k, v in zip(keys, combo):
            setattr(cfg, k, v)
        cfg.name = "_".join(f"{k}={v}" for k, v in zip(keys, combo))
        res = run_backtest(md, cfg)
        s = M.summary(res.daily_ret, cfg.risk_free_annual)
        row = {**dict(zip(keys, combo)), **s,
               "Turnover": M.turnover_stats(res.holdings_history)}
        rows.append(row)
        if progress:
            print(f"[{n}/{len(combos)}] {cfg.name:48s} "
                  f"Sharpe={s['Sharpe']:.2f} CAGR={s['CAGR']*100:5.1f}% "
                  f"MaxDD={s['Max Drawdown']*100:6.1f}%")
    return pd.DataFrame(rows)


def best_configs(results: pd.DataFrame) -> dict:
    return {
        "max_sharpe": results.loc[results["Sharpe"].idxmax()].to_dict(),
        "max_cagr": results.loc[results["CAGR"].idxmax()].to_dict(),
        "min_drawdown": results.loc[results["Max Drawdown"].idxmax()].to_dict(),  # closest to 0
        "max_calmar": results.loc[results["Calmar"].idxmax()].to_dict(),
    }
