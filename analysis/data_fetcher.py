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


def fetch_klines(client: Client, symbol: str, timeframe: str, limit: int = 1000) -> pd.DataFrame | None: # limit 기본값 변경
    """
    바이낸스에서 K-line(캔들) 데이터를 가져와 pandas DataFrame으로 변환합니다.
    """
    try:
        klines = client.futures_klines(symbol=symbol, interval=timeframe, limit=limit)
        if not klines:
            return None

        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])

        # 데이터 타입 변환
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])

        return df[['open', 'high', 'low', 'close', 'volume']]

    except Exception as e:
        print(f"Error fetching klines for {symbol} ({timeframe}): {e}")
        return None
