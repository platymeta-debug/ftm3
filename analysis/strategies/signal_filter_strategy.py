# 파일명: analysis/strategies/signal_filter_strategy.py

import pandas as pd
from .base_strategy import BaseStrategy
from core.config_manager import config

class SignalFilterStrategy(BaseStrategy):
    """
    거래량과 변동성 지표를 사용하여 진입 신호의 유효성을 검증하는 필터 전략.
    """
    name = "신호 필터 전략"

    def analyze(self, data: pd.DataFrame) -> dict:
        """
        주어진 데이터의 마지막 캔들이 진입에 적합한지 검증합니다.
        :return: {'is_valid': bool, 'reason': str} 형태의 딕셔너리
        """
        last = data.iloc[-1]
        prev_data = data.iloc[-21:-1] # 직전 20개 캔들 데이터

        # 1. 거래량 필터
        avg_volume = prev_data['volume'].mean()
        if last['volume'] < avg_volume * config.volume_spike_factor:
            return {
                "is_valid": False,
                "reason": f"거래량 미달 (현재: {last['volume']:.0f} < 기준: {avg_volume * config.volume_spike_factor:.0f})"
            }

        # 2. 변동성 필터
        atr = last.get("ATRr_14") or last.get("ATR_14", 0)
        volatility_ratio = atr / last['close']
        if volatility_ratio > config.max_volatility_ratio:
            return {
                "is_valid": False,
                "reason": f"과도한 변동성 (현재: {volatility_ratio:.2%} > 기준: {config.max_volatility_ratio:.2%})"
            }

        # 모든 필터를 통과
        return {"is_valid": True, "reason": "모든 신호 필터 통과"}