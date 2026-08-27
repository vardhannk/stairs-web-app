"""
kite_data.py
============
Builds the live MarketData from Zerodha Kite for the CURRENT universe.

Note on bias: for LIVE forward trading you trade today's constituents, so using
the current Nifty500/BSE500 membership here is correct. Survivorship bias only
distorts *backtests* — keep your point-in-time list for that, this is for live.

Access token: Kite tokens expire daily. This reads the token from (in order)
  1. env var KITE_ACCESS_TOKEN
  2. a token file (default /opt/stairs-web-app/.kite_token)
Wire whichever your STAIRS app already uses for its other Kite calls.
"""
from __future__ import annotations
import os, time, datetime as dt
import pandas as pd

CACHE_DIR = "/opt/stairs-web-app/momentum_cache"
TOKEN_FILE = "/opt/stairs-web-app/.kite_token"
THROTTLE_S = 0.34
LIVE_LOOKBACK_DAYS = 500          # ~2y, enough for 200DMA + momentum
INDEX_TOKENS = {"NIFTY50": 256265, "NIFTY500": 268041}


def get_kite(api_key: str | None = None):
    from kiteconnect import KiteConnect
    api_key = api_key or os.environ.get("KITE_API_KEY", "kitemcp")
    token = os.environ.get("KITE_ACCESS_TOKEN")
    if not token and os.path.exists(TOKEN_FILE):
        token = open(TOKEN_FILE).read().strip()
    if not token:
        raise RuntimeError("No Kite access token (set KITE_ACCESS_TOKEN or "
                           f"write {TOKEN_FILE}). Tokens expire daily.")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    return kite


def _token_map(kite, symbols):
    inst = pd.DataFrame(kite.instruments("NSE"))
    inst = inst[(inst["instrument_type"] == "EQ") &
                (inst["tradingsymbol"].isin(symbols))]
    return dict(zip(inst["tradingsymbol"], inst["instrument_token"]))


def _fetch_daily(kite, token, start, end):
    for attempt in range(3):
        try:
            return kite.historical_data(token, start, end, "day")
        except Exception:                       # noqa
            if attempt == 2:
                return []
            time.sleep(1.0)


def _cached_series(kite, sym, token, end):
    """Incremental cache: load parquet, fetch only the tail, append."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = f"{CACHE_DIR}/{sym}.parquet"
    start = end - dt.timedelta(days=LIVE_LOOKBACK_DAYS)
    existing = pd.read_parquet(path) if os.path.exists(path) else None
    if existing is not None and len(existing):
        last = existing.index.max()
        if last.date() >= end.date():
            return existing.loc[start:]
        fetch_from = last - dt.timedelta(days=3)        # small overlap
    else:
        fetch_from = start
    candles = _fetch_daily(kite, token, fetch_from, end)
    time.sleep(THROTTLE_S)
    if not candles:
        return existing.loc[start:] if existing is not None else pd.DataFrame()
    new = pd.DataFrame(candles)
    new["date"] = pd.to_datetime(new["date"]).dt.tz_localize(None)
    new = new.set_index("date")
    df = (pd.concat([existing, new]) if existing is not None else new)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_parquet(path)
    return df.loc[start:]


def build_live_market_data(symbols, sectors_map, api_key=None) -> MarketData:  # type: ignore  # noqa
    from momentum.data import MarketData
    kite = get_kite(api_key)
    end = dt.datetime.now()
    tokens = _token_map(kite, symbols)

    closes, vvalue = {}, {}
    for sym in symbols:
        tok = tokens.get(sym)
        if not tok:
            continue
        df = _cached_series(kite, sym, tok, end)
        if df is None or df.empty:
            continue
        closes[sym] = df["close"]
        vvalue[sym] = df["close"] * df["volume"]

    index_levels = {}
    for name, tok in INDEX_TOKENS.items():
        df = _cached_series(kite, name, tok, end)
        if df is not None and not df.empty:
            index_levels[name] = df["close"]

    prices = pd.DataFrame(closes).sort_index()
    volume = pd.DataFrame(vvalue).reindex(prices.index)
    return MarketData(
        prices=prices, volume=volume,
        sectors={s: sectors_map.get(s, "UNKNOWN") for s in prices.columns},
        benchmarks=index_levels,
        market_index=index_levels.get("NIFTY50"),
    )
