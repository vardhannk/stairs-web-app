"""
config.py
=========
Central configuration for the momentum portfolio engine.

Every tunable in the brief lives here so a strategy run is fully described
by one Config object. Nothing else in the codebase hard-codes parameters.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Literal


# Trading-day constants (approx). Indian market ~ 250 sessions / year.
TRADING_DAYS_YEAR = 252
TRADING_DAYS_MONTH = 21


@dataclass
class Config:
    # ----- Universe ---------------------------------------------------------
    universe: Literal["bse500", "nifty500"] = "bse500"
    min_history_days: int = TRADING_DAYS_YEAR          # >= 1y price history
    min_adv_inr: float = 5_000_000                      # avg daily traded value floor (INR)
    adv_lookback_days: int = TRADING_DAYS_MONTH * 3     # window for ADV
    max_illiquid_gap_frac: float = 0.05                 # max frac of zero-volume / NaN days allowed

    # ----- Momentum model ---------------------------------------------------
    # "m1" = 12-1 classic | "m2" = dual momentum | "m3" = multi-factor composite
    model: Literal["m1", "m2", "m3"] = "m3"
    lookback_skip_days: int = TRADING_DAYS_MONTH        # the "-1" month skip (12-1)
    lookback_long_days: int = TRADING_DAYS_YEAR         # 12m
    lookback_mid_days: int = TRADING_DAYS_MONTH * 6     # 6m
    lookback_short_days: int = TRADING_DAYS_MONTH * 3   # 3m
    high_lookback_days: int = TRADING_DAYS_YEAR         # 52-week high window

    # Composite weights for Model 3 (need not sum to 1; normalised internally)
    w_ret_3m: float = 1.0
    w_ret_6m: float = 1.0
    w_ret_12m: float = 1.0
    w_rs_rank: float = 1.0
    w_dist_52w_high: float = 1.0

    # ----- Trend filters ----------------------------------------------------
    use_market_filter: bool = True
    market_ma_days: int = 50                            # Nifty50 > 50DMA to be invested
    use_stock_filter: bool = True
    stock_ma_long: int = 200                            # price > 200DMA
    stock_ma_short: int = 50                            # price > 50DMA
    require_hh_hl: bool = True                           # higher-highs / higher-lows structure
    hh_hl_window: int = TRADING_DAYS_MONTH              # swing window for HH/HL test
    hh_hl_lookback: int = TRADING_DAYS_MONTH * 3        # how far back to confirm the uptrend

    # ----- Portfolio construction ------------------------------------------
    top_n: int = 20
    weighting: Literal["equal", "rank", "vol"] = "equal"
    vol_lookback_days: int = TRADING_DAYS_MONTH * 3     # vol estimate for vol-weighting

    # ----- Risk management --------------------------------------------------
    max_position_weight: float = 0.10                   # hard cap per name
    sector_cap: float = 0.30                            # max weight per sector
    target_vol_annual: float | None = 0.15              # None disables vol targeting
    target_vol_lookback: int = TRADING_DAYS_MONTH * 3
    max_leverage: float = 1.0                           # gross exposure ceiling after vol scaling

    stop_type: Literal["none", "pct", "atr"] = "pct"
    stop_pct: float = 0.20                              # trailing % stop (e.g. 0.15 / 0.20)
    atr_period: int = 14
    atr_mult: float = 3.0                               # trailing ATR stop multiple

    # ----- Rebalancing ------------------------------------------------------
    rebalance: Literal["weekly", "monthly"] = "monthly"

    # ----- Costs & cash -----------------------------------------------------
    cost_bps: float = 15.0                              # round-trip cost per unit turnover (bps)
    slippage_bps: float = 5.0
    risk_free_annual: float = 0.06                      # ~ India T-bill; used for Sharpe & dual mom
    initial_capital: float = 1_000_000.0

    # ----- Bookkeeping ------------------------------------------------------
    name: str = "momentum_default"

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def rf_daily(self) -> float:
        return (1 + self.risk_free_annual) ** (1 / TRADING_DAYS_YEAR) - 1
