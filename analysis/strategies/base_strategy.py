# analysis/strategies/base_strategy.py (수정)

from abc import ABC, abstractmethod
import pandas as pd

class BaseStrategy(ABC):
    """
    모든 트레이딩 전략이 상속받아야 할 추상 기본 클래스입니다.
    각 전략은 이 클래스의 'analyze' 메소드를 반드시 구현해야 합니다.
    """
    
    # 클래스 변수로 전략의 이름을 정의합니다.
    name = "Base Strategy"

    @abstractmethod
    def analyze(self, data: pd.DataFrame) -> pd.Series:
        """
        주어진 데이터(OHLCV + 모든 지표)를 분석하여
        진입/청산 결정을 위한 최종 신호(signal)를 담은 Series를 반환합니다.
        
        반환되는 Series에는 최소한 'signal'이라는 컬럼이 포함되어야 합니다.
        (예: 1.0 = 매수, -1.0 = 매도, 0 = 관망)
        """
        pass
