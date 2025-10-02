# 파일명: analysis/strategies/oscillator_strategy.py

import pandas as pd
from .base_strategy import BaseStrategy

class OscillatorStrategy(BaseStrategy):
    """
    RSI, Stochastic, MFI 지표를 사용하여 과매수/과매도 상태를 분석하고 점수를 반환합니다.
    """
    name = "오실레이터 전략"

    def analyze(self, data: pd.DataFrame) -> dict:
        """과매수/과매도 지표를 종합하여 점수를 계산합니다."""
        scores = {"자금": 0, "오실": 0}
        last = data.iloc[-1]

        # 데이터 유효성 검사
        required_cols = ["MFI_14", "OBV", "RSI_14", "STOCHk_14_3_3"]
        if any(pd.isna(last.get(col)) for col in required_cols):
            return scores

        # MFI 및 OBV를 이용한 자금 흐름 점수
        obv_ema = data['OBV'].ewm(span=20, adjust=False).mean().iloc[-1]
        if last["MFI_14"] < 20 or last["OBV"] > obv_ema:
            scores["자금"] = 1
        elif last["MFI_14"] > 80 or last["OBV"] < obv_ema:
            scores["자금"] = -1

        # RSI 및 Stochastic을 이용한 과매수/과매도 점수
        if last["RSI_14"] < 30 and last["STOCHk_14_3_3"] < 20:
            scores["오실"] = 2  # 과매도
        elif last["RSI_14"] > 70 and last["STOCHk_14_3_3"] > 80:
            scores["오실"] = -2 # 과매수

        return scores