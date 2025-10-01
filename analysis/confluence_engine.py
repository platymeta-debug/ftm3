# 파일명: analysis/confluence_engine.py (전체 최종 수정안)

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

    def _calculate_bias_score(self, df: pd.DataFrame, timeframe: str) -> int:
        """단일 타임프레임의 지표들을 바탕으로 편향 점수를 계산합니다."""
        if df.empty or len(df) < 200: # 데이터가 충분하지 않으면 계산하지 않음
            print(f"[{timeframe}] 데이터 부족으로 점수 계산 건너뜀 (데이터 수: {len(df)})")
            return 0
            
        score = 0
        last_row = df.iloc[-1]

        # --- 모든 지표 값을 안전하게 가져오기 ---
        close_price = last_row.get('close')
        ema20 = last_row.get('EMA_20')
        ema50 = last_row.get('EMA_50')
        ema200 = last_row.get('EMA_200')
        rsi_value = last_row.get('RSI_14')
        tenkan_sen = last_row.get('ITS_9')
        kijun_sen = last_row.get('IKS_26')
        senkou_a = last_row.get('ISA_9')
        senkou_b = last_row.get('ISB_26')

        # --- 디버깅 로그: 현재 지표 값들 출력 ---
        print(f"--- [{timeframe}] 지표 값 ---")
        print(f"Close: {close_price:.2f}, EMA20: {ema20:.2f}, EMA50: {ema50:.2f}, EMA200: {ema200:.2f}")
        print(f"RSI: {rsi_value:.2f}, Tenkan: {tenkan_sen:.2f}, Kijun: {kijun_sen:.2f}, SpanA: {senkou_a:.2f}, SpanB: {senkou_b:.2f}")
        
        # --- 값이 존재할 때만 점수 계산 ---
        trend_score, rsi_score, ichimoku_score = 0, 0, 0

        # 1. 추세 점수 (EMA 배열)
        if all(v is not None and not math.isnan(v) for v in [close_price, ema20, ema50, ema200]):
            if ema20 > ema50 > ema200: trend_score = 2
            elif ema20 < ema50 < ema200: trend_score = -2
            elif close_price > ema50: trend_score = 1
            elif close_price < ema50: trend_score = -1

        # 2. 모멘텀 점수 (RSI)
        if rsi_value is not None and not math.isnan(rsi_value):
            if rsi_value > 70: rsi_score = -1
            elif rsi_value < 30: rsi_score = 1

        # 3. 이치모쿠 클라우드 점수
        if all(v is not None and not math.isnan(v) for v in [close_price, tenkan_sen, kijun_sen, senkou_a, senkou_b]):
            if close_price > senkou_a and close_price > senkou_b:
                if tenkan_sen > kijun_sen: ichimoku_score = 2
                else: ichimoku_score = 1
            elif close_price < senkou_a and close_price < senkou_b:
                if tenkan_sen < kijun_sen: ichimoku_score = -2
                else: ichimoku_score = -1
        
        score = trend_score + rsi_score + ichimoku_score
        print(f"점수 계산: Trend({trend_score}), RSI({rsi_score}), Ichimoku({ichimoku_score}) -> 합계: {score}")
        return score

    def analyze(self, symbol: str) -> [Tuple, Dict]:
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

            tf_scores[timeframe] = self._calculate_bias_score(indicators, timeframe)
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
        if not config.timeframes: return 0.0
        primary_tf = config.timeframes # 가장 상위 타임프레임의 ATR을 사용
        row = tf_rows.get(primary_tf)
        if row is None: return 0.0
        
        for key in ("ATR_14", "ATRr_14"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not math.isnan(value):
                return float(value)
        return 0.0
