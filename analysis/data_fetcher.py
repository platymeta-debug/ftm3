"""Utilities for retrieving market data from Binance."""

from __future__ import annotations

from typing import Optional

import pandas as pd
from binance.client import Client


_KLINE_COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
]


def fetch_klines(
    client: Client,
    symbol: str,
    timeframe: str,
    limit: int = 500,
) -> Optional[pd.DataFrame]:
    """Fetch OHLCV candle data from Binance and return it as a DataFrame."""

    params = {"symbol": symbol, "interval": timeframe, "limit": limit}
    try:
        if hasattr(client, "futures_klines"):
            klines = client.futures_klines(**params)
        else:
            klines = client.get_klines(**params)
    except Exception as exc:  # pragma: no cover - network interaction
        print(f"Error fetching klines for {symbol} ({timeframe}): {exc}")
        return None

    if not klines:
        return None

    frame = pd.DataFrame(klines, columns=_KLINE_COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms")
    frame.set_index("timestamp", inplace=True)

    numeric_cols = ["open", "high", "low", "close", "volume"]
    frame[numeric_cols] = frame[numeric_cols].apply(pd.to_numeric, errors="coerce")

    return frame[numeric_cols].dropna(how="any")
