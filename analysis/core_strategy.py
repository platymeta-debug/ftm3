import pandas as pd
from enum import Enum

# MarketRegime Enum은 그대로 유지
class MarketRegime(Enum):
    BULL_TREND = "강세 추세"
    BEAR_TREND = "약세 추세"
    SIDEWAYS = "횡보"

def diagnose_market_regime(indicator_data: pd.Series, adx_threshold: float) -> MarketRegime:
    """
    주어진 지표 데이터를 기반으로 현재 시장 체제를 진단하는 '공용 함수'.
    DB에 의존하지 않고, 오직 데이터(Series)만으로 판단을 반환합니다.
    """
    # --- ▼▼▼ [핵심 수정] DB 접속 코드를 모두 삭제하고, 데이터 직접 사용 ▼▼▼ ---
    adx = indicator_data.get('adx_4h')
    is_above_ema200 = indicator_data.get('is_above_ema200_1d')

    # 필요한 데이터가 없으면 '횡보'로 판단
    if pd.isna(adx) or pd.isna(is_above_ema200):
        return MarketRegime.SIDEWAYS

    if adx > adx_threshold:
        return MarketRegime.BULL_TREND if is_above_ema200 else MarketRegime.BEAR_TREND
    else:
        return MarketRegime.SIDEWAYS
    # --- ▲▲▲ [핵심 수정] ▲▲▲ ---

# 향후 신호 품질 검증 등 다른 공용 로직도 이곳에 추가될 수 있습니다.
