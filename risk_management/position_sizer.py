# 파일명: risk_management/position_sizer.py (최종본)

"""Risk management helper for determining order quantities."""

from __future__ import annotations

from binance.client import Client

from core.config_manager import config


class PositionSizer:
    def __init__(self, client: Client):
        self.client = client
        print("포지션 사이저가 초기화되었습니다.")

    def calculate_position_size(self, symbol: str, entry_price: float, atr: float) -> float:
        """
        리스크 설정에 기반하여 적절한 포지션 크기(수량)를 계산합니다.
        
        Phase 2에서는.env에 설정된 고정 수량을 사용합니다.
        향후 Phase에서는 ATR과 계좌 잔고를 이용한 동적 수량 계산 로직으로 발전될 것입니다.
        """
        
        # TODO: ATR과 계좌 잔고를 이용한 동적 수량 계산 로직 구현
        # 예: max_risk = account_balance * risk_per_trade_pct
        #     stop_loss_distance = atr * atr_multiplier
        #     quantity = max_risk / stop_loss_distance

        return config._get_float('TRADE_QUANTITY', 0.001)