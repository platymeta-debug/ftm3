# analysis/strategies/momentum_strategy.py

import pandas as pd
from .base_strategy import BaseStrategy

class MomentumStrategy(BaseStrategy):
    """
    추세의 가속도를 측정하여 추세 방향으로의 진입을 확인하는 모멘텀 전략.
    기존 추세 전략의 신뢰도를 높이는 필터 역할을 합니다.
    """
    name = "모멘텀 전략"

    def __init__(self, params: dict):
        self.p = params
        self.smi_col = f"SMI_5_20_5" # Stochastic Momentum Index
        self.roc_col = f"ROC_10"     # Rate of Change

    def analyze(self, data: pd.DataFrame) -> dict:
        scores = {"Momentum": 0}
        last = data.iloc[-1]

        # pandas_ta 최신 버전은 SMI 컬럼명에 Signal이 붙을 수 있음
        smi_signal_col = f"{self.smi_col}_S"
        
        # 컬럼 존재 여부 확인
        actual_smi_col = self.smi_col if self.smi_col in data.columns else (smi_signal_col if smi_signal_col in data.columns else None)
        
        if not actual_smi_col or pd.isna(last.get(actual_smi_col)) or pd.isna(last.get(self.roc_col)):
            return scores

        # SMI가 0선 위에 있고 ROC가 양수이면 상승 모멘텀
        is_bullish_momentum = last[actual_smi_col] > 0 and last[self.roc_col] > self.p.get('roc_threshold', 0)
        
        # SMI가 0선 아래에 있고 ROC가 음수이면 하락 모멘텀
        is_bearish_momentum = last[actual_smi_col] < 0 and last[self.roc_col] < -self.p.get('roc_threshold', 0)

        if is_bullish_momentum:
            scores["Momentum"] = self.p.get("score_momentum_buy", 3)
        elif is_bearish_momentum:
            scores["Momentum"] = self.p.get("score_momentum_sell", -3)
            
        return scores