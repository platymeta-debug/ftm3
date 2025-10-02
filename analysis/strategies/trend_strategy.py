# analysis/strategies/trend_strategy.py (설정 파일 적용)

import pandas as pd
from .base_strategy import BaseStrategy

class TrendStrategy(BaseStrategy):
    name = "추세 전략"

    def __init__(self, params: dict):
        self.ema_short = params.get("ema_short", 20)
        self.ema_long = params.get("ema_long", 50)
        self.score = params.get("score_strong_trend", 2)
        # EMA 컬럼명을 동적으로 생성
        self.ema_short_col = f"EMA_{self.ema_short}"
        self.ema_long_col = f"EMA_{self.ema_long}"

    def analyze(self, data: pd.DataFrame) -> dict:
        scores = {"추세": 0}
        last = data.iloc[-1]

        if pd.isna(last.get(self.ema_short_col)) or pd.isna(last.get(self.ema_long_col)):
            return scores

        if last["close"] > last[self.ema_short_col] > last[self.ema_long_col]:
            scores["추세"] = self.score
        elif last["close"] < last[self.ema_short_col] < last[self.ema_long_col]:
            scores["추세"] = -self.score

        return scores
