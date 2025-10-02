# analysis/strategies/oscillator_strategy.py (설정 파일 적용)

import pandas as pd
from .base_strategy import BaseStrategy

class OscillatorStrategy(BaseStrategy):
    name = "오실레이터 전략"

    def __init__(self, params: dict):
        self.p = params # 모든 파라미터를 self.p에 저장
        self.obv_ema_col = f"OBVe_{self.p.get('obv_ema_period', 20)}"
        # 필요한 지표 컬럼명 미리 정의
        self.mfi_col = f"MFI_{self.p.get('mfi_period', 14)}"
        self.rsi_col = f"RSI_{self.p.get('rsi_period', 14)}"
        self.stoch_col = f"STOCHk_{self.p.get('stoch_k',14)}_{self.p.get('stoch_d',3)}_{self.p.get('stoch_smooth_k',3)}"

    def analyze(self, data: pd.DataFrame) -> dict:
        scores = {"자금": 0, "오실": 0}
        last = data.iloc[-1]

        required_cols = [self.mfi_col, "OBV", self.rsi_col, self.stoch_col]
        if any(pd.isna(last.get(col)) for col in required_cols):
            return scores

        obv_ema = data['OBV'].ewm(span=self.p.get('obv_ema_period', 20), adjust=False).mean().iloc[-1]
        if last[self.mfi_col] < self.p.get('mfi_oversold', 20) or last["OBV"] > obv_ema:
            scores["자금"] = self.p.get('score_inflow', 1)
        elif last[self.mfi_col] > self.p.get('mfi_overbought', 80) or last["OBV"] < obv_ema:
            scores["자금"] = self.p.get('score_outflow', -1)

        if last[self.rsi_col] < self.p.get('rsi_oversold', 30) and last[self.stoch_col] < self.p.get('stoch_oversold', 20):
            scores["오실"] = self.p.get('score_oversold', 2)
        elif last[self.rsi_col] > self.p.get('rsi_overbought', 70) and last[self.stoch_col] > self.p.get('stoch_overbought', 80):
            scores["오실"] = self.p.get('score_overbought', -2)

        return scores
