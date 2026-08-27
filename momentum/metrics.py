"""
metrics.py
==========
Performance + portfolio analytics. All return-based metrics take a daily
return Series indexed by date.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from .config import TRADING_DAYS_YEAR


def equity_from_returns(daily_ret: pd.Series, initial: float = 1.0) -> pd.Series:
    return (1 + daily_ret.fillna(0)).cumprod() * initial


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2:
        return np.nan
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return np.nan
    return (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1


def ann_return(daily_ret: pd.Series) -> float:
    return daily_ret.mean() * TRADING_DAYS_YEAR


def ann_vol(daily_ret: pd.Series) -> float:
    return daily_ret.std() * np.sqrt(TRADING_DAYS_YEAR)


def sharpe(daily_ret: pd.Series, rf_annual: float) -> float:
    rf_d = (1 + rf_annual) ** (1 / TRADING_DAYS_YEAR) - 1
    excess = daily_ret - rf_d
    sd = excess.std()
    return np.sqrt(TRADING_DAYS_YEAR) * excess.mean() / sd if sd > 0 else np.nan


def sortino(daily_ret: pd.Series, rf_annual: float) -> float:
    rf_d = (1 + rf_annual) ** (1 / TRADING_DAYS_YEAR) - 1
    excess = daily_ret - rf_d
    downside = excess[excess < 0]
    dd = downside.std()
    return np.sqrt(TRADING_DAYS_YEAR) * excess.mean() / dd if dd > 0 else np.nan


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def max_drawdown(equity: pd.Series) -> float:
    return drawdown_series(equity).min()


def calmar(equity: pd.Series) -> float:
    mdd = abs(max_drawdown(equity))
    c = cagr(equity)
    return c / mdd if mdd > 0 else np.nan


def win_rate(daily_ret: pd.Series) -> float:
    nz = daily_ret[daily_ret != 0]
    return (nz > 0).mean() if len(nz) else np.nan


def monthly_return_table(daily_ret: pd.Series) -> pd.DataFrame:
    m = (1 + daily_ret.fillna(0)).resample("ME").prod() - 1
    df = m.to_frame("ret")
    df["year"] = df.index.year
    df["month"] = df.index.month
    table = df.pivot_table(index="year", columns="month", values="ret")
    table.columns = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][:table.shape[1]]
    table["Year"] = (1 + df.set_index(df.index)["ret"]).groupby(df["year"]).prod() - 1
    return table


def summary(daily_ret: pd.Series, rf_annual: float) -> dict:
    eq = equity_from_returns(daily_ret)
    return {
        "CAGR": cagr(eq),
        "Annual Return": ann_return(daily_ret),
        "Volatility": ann_vol(daily_ret),
        "Sharpe": sharpe(daily_ret, rf_annual),
        "Sortino": sortino(daily_ret, rf_annual),
        "Calmar": calmar(eq),
        "Max Drawdown": max_drawdown(eq),
        "Win Rate (daily)": win_rate(daily_ret),
    }


# ---- portfolio analytics (operate on trade log / holdings history) ---------

def turnover_stats(holdings_history: dict) -> float:
    """Average one-way turnover per rebalance."""
    dates = sorted(holdings_history)
    if len(dates) < 2:
        return np.nan
    tos = []
    for a, b in zip(dates[:-1], dates[1:]):
        wa = holdings_history[a]
        wb = holdings_history[b]
        idx = wa.index.union(wb.index)
        tos.append((wb.reindex(idx, fill_value=0) - wa.reindex(idx, fill_value=0)).abs().sum() / 2)
    return float(np.mean(tos))


def trades_per_year(trade_log: pd.DataFrame, equity: pd.Series) -> float:
    if trade_log.empty:
        return 0.0
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    return len(trade_log) / years if years > 0 else np.nan


def avg_holding_period_days(trade_log: pd.DataFrame) -> float:
    """Match buys and sells per ticker FIFO to estimate holding days."""
    if trade_log.empty:
        return np.nan
    spans = []
    for tkr, g in trade_log.groupby("ticker"):
        g = g.sort_values("date")
        open_lots = []                      # FIFO queue of open buy dates
        for _, row in g.iterrows():
            if row["side"] == "BUY":
                open_lots.append(pd.Timestamp(row["date"]))
            elif row["side"] == "SELL" and open_lots:
                b = open_lots.pop(0)        # close oldest lot
                spans.append((pd.Timestamp(row["date"]) - b).days)
    return float(np.mean(spans)) if spans else np.nan
