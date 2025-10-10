# analysis/strategies/signal_filter_strategy.py (최종 수정)

import pandas as pd
from .base_strategy import BaseStrategy
# --- ▼▼▼ [핵심 수정] config 직접 임포트 삭제 ▼▼▼ ---
# from core.config_manager import config # <- 이 줄을 삭제합니다.
# --- ▲▲▲ [핵심 수정] ---

class SignalFilterStrategy(BaseStrategy):
    """
    거래량과 변동성 지표를 사용하여 진입 신호의 유효성을 검증하는 필터 전략.
    """
    name = "신호 필터 전략"

    # --- ▼▼▼ [핵심 수정] 생성자에서 config 객체를 주입받도록 변경 ▼▼▼ ---
    def __init__(self, config):
        self.config = config
    # --- ▲▲▲ [핵심 수정] ---

    def analyze(self, data: pd.DataFrame) -> dict:
        """
        주어진 데이터의 마지막 캔들이 진입에 적합한지 검증합니다.
        :return: {'is_valid': bool, 'reason': str} 형태의 딕셔너리
        """
        last = data.iloc[-1]
        prev_data = data.iloc[-21:-1] # 직전 20개 캔들 데이터

        # --- ▼▼▼ [핵심 수정] 전역 config 대신 self.config 사용 ▼▼▼ ---
        # 1. 거래량 필터
        # .env에 volume_spike_factor, max_volatility_ratio가 없으면 기본값을 사용하도록 .get()으로 변경
        volume_spike_factor = getattr(self.config, 'volume_spike_factor', 1.5)
        max_volatility_ratio = getattr(self.config, 'max_volatility_ratio', 0.05)

        avg_volume = prev_data['volume'].mean()
        if last['volume'] < avg_volume * volume_spike_factor:
            return {
                "is_valid": False,
                "reason": f"거래량 미달 (현재: {last['volume']:.0f} < 기준: {avg_volume * volume_spike_factor:.0f})"
            }

        # 2. 변동성 필터
        atr = last.get("ATRr_14") or last.get("ATR_14", 0)
        volatility_ratio = (atr / last['close']) if last['close'] > 0 else 0
        if volatility_ratio > max_volatility_ratio:
            return {
                "is_valid": False,
                "reason": f"과도한 변동성 (현재: {volatility_ratio:.2%} > 기준: {max_volatility_ratio:.2%})"
            }
        # --- ▲▲▲ [핵심 수정] ---

        # 모든 필터를 통과
        return {"is_valid": True, "reason": "모든 신호 필터 통과"}
