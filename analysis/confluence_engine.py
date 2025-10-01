# 파일명: analysis/confluence_engine.py (전체 수정안)

"""Hierarchical confluence engine responsible for scoring market bias."""

from __future__ import annotations

import math
from typing import Dict, Tuple

import pandas as pd
from binance.client import Client

from core.config_manager import config
from. import data_fetcher, indicator_calculator


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

    # analysis/confluence_engine.py 파일의 _calculate_bias_score 함수를 아래 내용으로 교체하세요.

    def _calculate_bias_score(self, df: pd.DataFrame) -> int:
        """단일 타임프레임의 지표들을 바탕으로 편향 점수를 계산합니다."""
        if df.empty:
            return 0
            
        score = 0
        last_row = df.iloc[-1]

        # 1. 추세 점수 (EMA 배열)
        if last_row['EMA_20'] > last_row['EMA_50'] and last_row['EMA_50'] > last_row['EMA_200']:
            score += 2 # 강력한 정배열
        elif last_row['EMA_20'] < last_row['EMA_50'] and last_row['EMA_50'] < last_row['EMA_200']:
            score -= 2 # 강력한 역배열
        elif last_row['close'] > last_row['EMA_50']:
            score += 1 # 상승 추세
        elif last_row['close'] < last_row['EMA_50']:
            score -= 1 # 하락 추세

        # 2. 모멘텀 점수 (RSI)
        rsi_value = last_row.get('RSI_14') # pandas-ta 버전에 따라 컬럼명이 다를 수 있음
        if rsi_value is not None:
            if rsi_value > 70: score -= 1 # 과매수
            elif rsi_value < 30: score += 1 # 과매도

        # 3. 이치모쿠 클라우드 점수
        # pandas-ta가 생성하는 컬럼명(ITS_9, IKS_26 등)을 사용
        tenkan_sen = last_row.get('ITS_9')
        kijun_sen = last_row.get('IKS_26')
        senkou_a = last_row.get('ISA_9')
        senkou_b = last_row.get('ISB_26')

        if all(v is not None for v in [tenkan_sen, kijun_sen, senkou_a, senkou_b]):
            if last_row['close'] > senkou_a and last_row['close'] > senkou_b: # 구름대 위
                if tenkan_sen > kijun_sen: # 전환선 > 기준선 (강세)
                    score += 2
                else:
                    score += 1
            elif last_row['close'] < senkou_a and last_row['close'] < senkou_b: # 구름대 아래
                if tenkan_sen < kijun_sen: # 전환선 < 기준선 (약세)
                    score -= 2
                else:
                    score -= 1
        
        return score

    @staticmethod
    def _atr_from_row(row: pd.Series) -> float:
        for key in ("ATR_14", "ATRr_14"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not math.isnan(value):
                return float(value)
        return 0.0

    def analyze(self, symbol: str) -> Tuple:
        tf_scores: Dict[str, int] = {}
        tf_rows: Dict = {}

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
            (tf_scores.get("4h", 0) > 0 and tf_scores.get("1d", 0) > 0)
            or (tf_scores.get("4h", 0) < 0 and tf_scores.get("1d", 0) < 0)
        ):
            final_score *= 1.2

        return final_score, tf_scores, tf_rows

    def extract_atr(self, tf_rows: Dict) -> float:
        if not config.timeframes:
            return 0.0
        primary_tf = config.timeframes
        row = tf_rows.get(primary_tf)
        if row is None:
            return 0.0
        return self._atr_from_row(row)
