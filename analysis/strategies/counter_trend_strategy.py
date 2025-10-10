# analysis/strategies/counter_trend_strategy.py

import pandas as pd
from .base_strategy import BaseStrategy

class CounterTrendStrategy(BaseStrategy):
    """
    과매수/과매도 구간에서의 반전을 노리는 역추세 전략.
    하락장 또는 단기 조정 구간에서 유효한 신호를 포착하는 것을 목표로 합니다.
    """
    name = "역추세 전략"

    def __init__(self, params: dict):
        self.p = params
        self.williams_col = f"WILLR_{self.p.get('williams_period', 14)}"
        self.rsi_col = f"RSI_{self.p.get('rsi_period', 14)}"

    def analyze(self, data: pd.DataFrame) -> dict:
        scores = {"Counter_Trend": 0}
        last = data.iloc[-1]

        required_cols = [self.williams_col, self.rsi_col]
        if any(pd.isna(last.get(col)) for col in required_cols):
            return scores

        is_overbought = last[self.williams_col] > self.p.get('williams_overbought', -20) and \
                        last[self.rsi_col] > self.p.get('rsi_overbought', 70)
        
        is_oversold = last[self.williams_col] < self.p.get('williams_oversold', -80) and \
                      last[self.rsi_col] < self.p.get('rsi_oversold', 30)

        if is_overbought:
            scores["Counter_Trend"] = self.p.get("score_counter_sell", -4) # 매도 (short) 신호
        elif is_oversold:
            scores["Counter_Trend"] = self.p.get("score_counter_buy", 4)  # 매수 (long) 신호
            
        return scores