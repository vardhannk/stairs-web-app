"""
reporting.py
============
Builds the required deliverables from a BacktestResult:
  - performance summary table (strategy vs benchmarks)
  - equity curve PNG
  - drawdown PNG
  - monthly returns heatmap PNG
  - trade log / holdings CSV exports
  - Excel workbook with everything
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm

from . import metrics as M
from .engine import BacktestResult


def benchmark_daily_ret(level: pd.Series, calendar: pd.DatetimeIndex) -> pd.Series:
    return level.reindex(calendar).ffill().pct_change().fillna(0)


def performance_table(res: BacktestResult, benchmarks: dict) -> pd.DataFrame:
    rf = res.cfg.risk_free_annual
    cols = {}
    cols["Strategy"] = M.summary(res.daily_ret, rf)
    for name, lvl in benchmarks.items():
        bdr = benchmark_daily_ret(lvl, res.equity.index)
        cols[name] = M.summary(bdr, rf)
    df = pd.DataFrame(cols)
    # portfolio analytics (strategy only)
    extra = {
        "Avg Turnover / Rebal": M.turnover_stats(res.holdings_history),
        "Trades / Year": M.trades_per_year(res.trade_log, res.equity),
        "Avg Holding (days)": M.avg_holding_period_days(res.trade_log),
        "Rebalances": len(res.holdings_history),
    }
    for k, v in extra.items():
        df.loc[k, "Strategy"] = v
    return df


def plot_equity(res: BacktestResult, benchmarks: dict, path: str):
    fig, ax = plt.subplots(figsize=(11, 5.2))
    base = res.equity / res.equity.iloc[0]
    ax.plot(base.index, base.values, lw=1.8, label="Strategy", color="#1f4e79")
    for name, lvl in benchmarks.items():
        b = lvl.reindex(res.equity.index).ffill()
        b = b / b.iloc[0]
        ax.plot(b.index, b.values, lw=1.2, alpha=0.8, label=name)
    ax.set_yscale("log")
    ax.set_title("Equity Curve (log scale, growth of 1)")
    ax.set_ylabel("Growth multiple")
    ax.legend(frameon=False)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_drawdown(res: BacktestResult, path: str):
    dd = M.drawdown_series(res.equity) * 100
    fig, ax = plt.subplots(figsize=(11, 3.6))
    ax.fill_between(dd.index, dd.values, 0, color="#c0392b", alpha=0.55)
    ax.set_title(f"Drawdown  (max {dd.min():.1f}%)")
    ax.set_ylabel("Drawdown %")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_monthly_heatmap(res: BacktestResult, path: str):
    table = M.monthly_return_table(res.daily_ret)
    months = [c for c in table.columns if c != "Year"]
    data = table[months] * 100
    fig, ax = plt.subplots(figsize=(11, max(3, 0.42 * len(data) + 1.5)))
    norm = TwoSlopeNorm(vmin=np.nanmin(data.values) if data.size else -1,
                        vcenter=0,
                        vmax=np.nanmax(data.values) if data.size else 1)
    im = ax.imshow(data.values, cmap="RdYlGn", norm=norm, aspect="auto")
    ax.set_xticks(range(len(months)), months)
    ax.set_yticks(range(len(data.index)), data.index)
    for y in range(data.shape[0]):
        for x in range(data.shape[1]):
            v = data.values[y, x]
            if not np.isnan(v):
                ax.text(x, y, f"{v:.1f}", ha="center", va="center", fontsize=7)
    ax.set_title("Monthly Returns Heatmap (%)")
    fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def holdings_long(res: BacktestResult) -> pd.DataFrame:
    rows = []
    for date, w in res.holdings_history.items():
        for t, wt in w.items():
            rows.append(dict(date=date, ticker=t, weight=round(float(wt), 5)))
    return pd.DataFrame(rows)


def export_excel(res: BacktestResult, benchmarks: dict, path: str):
    perf = performance_table(res, benchmarks)
    monthly = M.monthly_return_table(res.daily_ret)
    hold = holdings_long(res)
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        perf.to_excel(xl, sheet_name="Performance")
        monthly.to_excel(xl, sheet_name="MonthlyReturns")
        res.trade_log.to_excel(xl, sheet_name="TradeLog", index=False)
        hold.to_excel(xl, sheet_name="Holdings", index=False)
        res.equity.to_frame("equity").to_excel(xl, sheet_name="EquityCurve")
