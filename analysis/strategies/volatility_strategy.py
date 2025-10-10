# analysis/strategies/volatility_strategy.py

import pandas as pd
from .base_strategy import BaseStrategy

class VolatilityStrategy(BaseStrategy):
    """
    볼린저 밴드와 켈트너 채널을 이용한 '스퀴즈 브레이크아웃' 전략.
    변동성이 극도로 낮아진 후 폭발하는 시점을 포착합니다.
    """
    name = "변동성 돌파 전략"

    def __init__(self, params: dict):
        self.p = params
        # 지표 컬럼명 설정
        self.bb_lower_col = f"BBL_{self.p.get('bb_len', 20)}_{self.p.get('bb_std', 2.0)}"
        self.bb_upper_col = f"BBU_{self.p.get('bb_len', 20)}_{self.p.get('bb_std', 2.0)}"
        self.kc_lower_col = f"KCL_{self.p.get('kc_len', 20)}_{self.p.get('kc_atr_mult', 2.0)}"
        self.kc_upper_col = f"KCU_{self.p.get('kc_len', 20)}_{self.p.get('kc_atr_mult', 2.0)}"

    def analyze(self, data: pd.DataFrame) -> dict:
        scores = {"Volatility_Squeeze": 0}
        last = data.iloc[-1]
        prev = data.iloc[-2]

        required_cols = [self.bb_lower_col, self.bb_upper_col, self.kc_lower_col, self.kc_upper_col]
        if any(pd.isna(last.get(col)) for col in required_cols):
            return scores

        # 1. 스퀴즈 상태 확인 (볼린저 밴드가 켈트너 채널 안쪽에 있는 상태)
        is_squeeze_now = (last[self.bb_lower_col] > last[self.kc_lower_col]) and \
                         (last[self.bb_upper_col] < last[self.kc_upper_col])
        
        # 2. 직전 캔들이 스퀴즈 상태였고, 현재 돌파가 일어났는지 확인
        was_squeeze_prev = (prev[self.bb_lower_col] > prev[self.kc_lower_col]) and \
                           (prev[self.bb_upper_col] < prev[self.kc_upper_col])

        if was_squeeze_prev and not is_squeeze_now:
            # 상향 돌파
            if last['close'] > last[self.bb_upper_col]:
                scores["Volatility_Squeeze"] = self.p.get("score_squeeze_breakout_up", 5)
            # 하향 돌파
            elif last['close'] < last[self.bb_lower_col]:
                scores["Volatility_Squeeze"] = self.p.get("score_squeeze_breakout_down", -5)
                
        return scores