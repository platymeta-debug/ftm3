import pandas as pd
from .base_strategy import BaseStrategy

class ConfluenceStrategy(BaseStrategy):
    """
    V4 업그레이드의 핵심이었던 'Macro-Tactical Confluence Engine'의 로직을
    재사용 가능한 전략 클래스로 구현한 버전입니다.
    """
    name = "Confluence Strategy V1"

    def __init__(self, open_th: float = 12.0, trend_weight: float = 4.0):
        """
        전략에 필요한 파라미터를 외부에서 주입받습니다.
        :param open_th: 포지션 진입을 위한 최소 점수 임계값
        :param trend_weight: 추세 점수에 적용될 가중치
        """
        self.open_th = open_th
        self.trend_weight = trend_weight

    def analyze(self, data: pd.DataFrame) -> pd.Series:
        """데이터를 분석하여 매매 신호를 생성합니다."""
        last = data.iloc[-1]
        
        trend_score, money_flow_score, oscillator_score = 0, 0, 0

        # 1. 점수 계산 (기존 백테스팅 로직과 동일)
        if all(k in last and pd.notna(last[k]) for k in ["EMA_20", "EMA_50", "close"]):
            if last['close'] > last['EMA_20'] > last['EMA_50']: trend_score = 2
            elif last['close'] < last['EMA_20'] < last['EMA_50']: trend_score = -2
            elif last['close'] > last['EMA_50']: trend_score = 1
            elif last['close'] < last['EMA_50']: trend_score = -1
        
        if all(k in last and pd.notna(last[k]) for k in ["MFI_14", "OBV"]):
            obv_ema = data['OBV'].ewm(span=20, adjust=False).mean().iloc[-1]
            if last['MFI_14'] > 80: money_flow_score -= 1
            if last['MFI_14'] < 20: money_flow_score += 1
            if last['OBV'] > obv_ema: money_flow_score += 1
            if last['OBV'] < obv_ema: money_flow_score -= 1

        if all(k in last and pd.notna(last[k]) for k in ["RSI_14", "STOCHk_14_3_3"]):
            if last['RSI_14'] < 30 and last['STOCHk_14_3_3'] < 20: oscillator_score = 2
            elif last['RSI_14'] > 70 and last['STOCHk_14_3_3'] > 80: oscillator_score = -2
            elif last['RSI_14'] < 40: oscillator_score = 1
            elif last['RSI_14'] > 60: oscillator_score = -1

        total_score = trend_score + money_flow_score + oscillator_score
        final_score = total_score * self.trend_weight

        # 2. 신호 생성
        signal = 0.0
        if final_score > self.open_th:
            signal = 1.0  # 매수 신호
        elif final_score < -self.open_th:
            signal = -1.0 # 매도 신호
        
        # 분석 결과를 Series 형태로 반환
        return pd.Series(
            {
                'signal': signal,
                'final_score': final_score,
                'trend_score': trend_score
            }
        )