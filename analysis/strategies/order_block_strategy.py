# analysis/strategies/order_block_strategy.py

import pandas as pd
from .base_strategy import BaseStrategy

class OrderBlockStrategy(BaseStrategy):
    """
    가격 행동(Price Action)을 기반으로 주요 지지/저항 구간인 오더블록을 탐지하는 전략.
    다른 지표 기반 전략들의 신뢰도를 크게 높여주는 시너지를 창출합니다.
    """
    name = "오더블록 전략"

    def __init__(self, params: dict):
        self.p = params
        self.lookback = self.p.get('lookback_period', 10) # 오더블록을 찾을 기간
        self.volume_factor = self.p.get('volume_factor', 1.5) # 평균 대비 거래량 배수

    def analyze(self, data: pd.DataFrame) -> dict:
        scores = {"Order_Block": 0}
        if len(data) < self.lookback:
            return scores

        recent_data = data.iloc[-self.lookback:]
        avg_volume = recent_data['volume'].mean()
        
        # Bullish Order Block (상승 지지) 탐지
        # : 하락 캔들 이후, 그 하락을 압도하는 거래량과 함께 더 큰 상승 장악 캔들이 나타나는 경우
        for i in range(1, len(recent_data)):
            prev_candle = recent_data.iloc[i-1]
            curr_candle = recent_data.iloc[i]
            
            is_bullish_engulfing = (prev_candle['close'] < prev_candle['open']) and \
                                   (curr_candle['close'] > curr_candle['open']) and \
                                   (curr_candle['close'] > prev_candle['open']) and \
                                   (curr_candle['open'] < prev_candle['close']) and \
                                   (curr_candle['volume'] > avg_volume * self.volume_factor)

            if is_bullish_engulfing:
                # 현재 가격이 이 오더블록(이전 음봉)의 가격대 근처에 있다면, 지지 신호
                if data.iloc[-1]['close'] >= prev_candle['low']:
                    scores["Order_Block"] = self.p.get("score_bullish_ob", 6)

        # Bearish Order Block (하락 저항) 탐지
        # : 상승 캔들 이후, 그 상승을 압도하는 거래량과 함께 더 큰 하락 장악 캔들이 나타나는 경우
        for i in range(1, len(recent_data)):
            prev_candle = recent_data.iloc[i-1]
            curr_candle = recent_data.iloc[i]

            is_bearish_engulfing = (prev_candle['close'] > prev_candle['open']) and \
                                   (curr_candle['close'] < curr_candle['open']) and \
                                   (curr_candle['close'] < prev_candle['open']) and \
                                   (curr_candle['open'] > prev_candle['close']) and \
                                   (curr_candle['volume'] > avg_volume * self.volume_factor)
            
            if is_bearish_engulfing:
                 # 현재 가격이 이 오더블록(이전 양봉)의 가격대 근처에 있다면, 저항 신호
                if data.iloc[-1]['close'] <= prev_candle['high']:
                    scores["Order_Block"] = self.p.get("score_bearish_ob", -6)
                    
        return scores