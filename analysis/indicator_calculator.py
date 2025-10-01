"""Indicator calculation helpers leveraging pandas-ta."""

from __future__ import annotations

from typing import List

import pandas as pd
import pandas_ta as ta


def _collect_frames(frames: List[pd.DataFrame | pd.Series]) -> pd.DataFrame:
    collected = [frame for frame in frames if frame is not None and not frame.empty]
    if not collected:
        return pd.DataFrame()
    return pd.concat(collected, axis=1)


def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Append technical indicators required by the confluence engine."""

    if df is None or df.empty:
        return pd.DataFrame()

    base = df.copy()
    frames: List[pd.DataFrame | pd.Series] = [base]

    ichimoku_df, ichimoku_span = ta.ichimoku(base["high"], base["low"], base["close"])
    frames.extend([ichimoku_df, ichimoku_span])

    frames.append(ta.ema(base["close"], length=20).rename("EMA_20"))
    frames.append(ta.ema(base["close"], length=50).rename("EMA_50"))
    frames.append(ta.ema(base["close"], length=200).rename("EMA_200"))

    rsi = ta.rsi(base["close"], length=14)
    if rsi is not None:
        frames.append(rsi.rename("RSI_14"))

    macd = ta.macd(base["close"])
    if macd is not None:
        frames.append(macd)

    bbands = ta.bbands(base["close"], length=20, std=2)
    if bbands is not None:
        frames.append(bbands)

    atr = ta.atr(base["high"], base["low"], base["close"], length=14)
    if atr is not None:
        frames.append(atr.rename("ATR_14"))

    adx = ta.adx(base["high"], base["low"], base["close"], length=14)
    if adx is not None:
        frames.append(adx)

    return _collect_frames(frames)
