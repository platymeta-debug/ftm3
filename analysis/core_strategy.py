import pandas as pd
from enum import Enum

# main.py에 있던 MarketRegime을 여기로 가져옵니다.
class MarketRegime(Enum):
    BULL_TREND = "강세 추세"
    BEAR_TREND = "약세 추세"
    SIDEWAYS = "횡보"

def diagnose_market_regime(session, symbol: str) -> MarketRegime:
    """[시장 진단] DB 데이터를 기반으로 현재 시장 체제를 진단합니다."""
    latest_signal_tuple = session.execute(
        select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc())
    ).first()

    if not latest_signal_tuple: return MarketRegime.SIDEWAYS 
    
    latest_signal = latest_signal_tuple[0]
    if latest_signal.adx_4h is None or getattr(latest_signal, 'is_above_ema200_1d', None) is None:
        return MarketRegime.SIDEWAYS

    if latest_signal.adx_4h > config.market_regime_adx_th:
        return MarketRegime.BULL_TREND if latest_signal.is_above_ema200_1d else MarketRegime.BEAR_TREND
    else:
        return MarketRegime.SIDEWAYS