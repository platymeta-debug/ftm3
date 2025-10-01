"""Hierarchical confluence engine responsible for scoring market bias."""

from __future__ import annotations

import math
from typing import Dict, Tuple

import pandas as pd
from binance.client import Client

from core.config_manager import config
from . import data_fetcher, indicator_calculator


class ConfluenceEngine:
    """Combine multi-timeframe indicators into a single confluence score."""

    def __init__(self, client: Client):
        self.client = client
        print("계층적 컨플루언스 엔진이 초기화되었습니다.")

    @staticmethod
    def _first_numeric(row: pd.Series, *keys: str) -> float | None:
        for key in keys:
            if key in row:
                value = row[key]
                if isinstance(value, (int, float)) and not math.isnan(value):
                    return float(value)
        return None

    def _calculate_bias_score(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0

        last_row = df.iloc[-1]
        score = 0

        close = self._first_numeric(last_row, "close")
        ema20 = self._first_numeric(last_row, "EMA_20")
        ema50 = self._first_numeric(last_row, "EMA_50")
        ema200 = self._first_numeric(last_row, "EMA_200")

        if all(value is not None for value in (ema20, ema50, ema200)):
            if ema20 > ema50 > ema200:
                score += 2
            elif ema20 < ema50 < ema200:
                score -= 2
        if close is not None and ema50 is not None:
            if close > ema50:
                score += 1
            elif close < ema50:
                score -= 1

        rsi = self._first_numeric(last_row, "RSI_14", "RSI")
        if rsi is not None:
            if rsi > 70:
                score -= 1
            elif rsi < 30:
                score += 1

        conversion = self._first_numeric(last_row, "ITS_9", "TENKAN_SEN")
        base_line = self._first_numeric(last_row, "IKS_26", "KIJUN_SEN")
        span_a = self._first_numeric(last_row, "ISA_9", "ISA_26", "SENKOU_A")
        span_b = self._first_numeric(last_row, "ISB_26", "ISB_52", "SENKOU_B")

        if close is not None and span_a is not None and span_b is not None:
            cloud_top = max(span_a, span_b)
            cloud_bottom = min(span_a, span_b)
            if close > cloud_top:
                score += 2 if conversion is not None and base_line is not None and conversion > base_line else 1
            elif close < cloud_bottom:
                score -= 2 if conversion is not None and base_line is not None and conversion < base_line else 1

        return score

    @staticmethod
    def _atr_from_row(row: pd.Series) -> float:
        for key in ("ATR_14", "ATRr_14", "ATR", "ATRr"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not math.isnan(value):
                return float(value)
        return 0.0

    def analyze(self, symbol: str) -> Tuple[float, Dict[str, int], Dict[str, pd.Series]]:
        tf_scores: Dict[str, int] = {}
        tf_rows: Dict[str, pd.Series] = {}

        for timeframe in config.timeframes:
            df = data_fetcher.fetch_klines(self.client, symbol, timeframe)
            if df is None or df.empty:
                tf_scores[timeframe] = 0
                continue

            indicators = indicator_calculator.calculate_all_indicators(df)
            if indicators.empty:
                tf_scores[timeframe] = 0
                continue

            tf_scores[timeframe] = self._calculate_bias_score(indicators)
            tf_rows[timeframe] = indicators.iloc[-1]

        final_score = 0.0
        for index, timeframe in enumerate(config.timeframes):
            weight = config.tf_vote_weights[index] if index < len(config.tf_vote_weights) else 1.0
            final_score += tf_scores.get(timeframe, 0) * weight

        if (
            tf_scores.get("4h", 0) > 0
            and tf_scores.get("1d", 0) > 0
            or tf_scores.get("4h", 0) < 0
            and tf_scores.get("1d", 0) < 0
        ):
            final_score *= 1.2

        return final_score, tf_scores, tf_rows

    def extract_atr(self, tf_rows: Dict[str, pd.Series]) -> float:
        if not config.timeframes:
            return 0.0
        primary_tf = config.timeframes[0]
        row = tf_rows.get(primary_tf)
        if row is None:
            return 0.0
        return self._atr_from_row(row)
