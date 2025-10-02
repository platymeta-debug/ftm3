# 파일명: analysis/strategies/trend_strategy.py

import pandas as pd
from .base_strategy import BaseStrategy

class TrendStrategy(BaseStrategy):
    """
    EMA 크로스오버와 배열을 기반으로 추세의 방향과 강도를 분석하여 점수를 반환합니다.
    """
    name = "추세 전략"

    def analyze(self, data: pd.DataFrame) -> dict:
        """EMA 배열을 분석하여 추세 점수를 계산합니다."""
        scores = {"추세": 0}
        last = data.iloc[-1]

        # 데이터 유효성 검사
        if pd.isna(last.get("EMA_20")) or pd.isna(last.get("EMA_50")):
            return scores

        # EMA 정배열/역배열에 따른 점수 부여
        if last["close"] > last["EMA_20"] > last["EMA_50"]:
            scores["추세"] = 2  # 강한 상승 추세
        elif last["close"] < last["EMA_20"] < last["EMA_50"]:
            scores["추세"] = -2 # 강한 하락 추세

        return scores