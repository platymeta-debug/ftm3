"""Risk management helper for determining order quantities."""

from __future__ import annotations

from binance.client import Client

from core.config_manager import config


class PositionSizer:
    def __init__(self, client: Client):
        self.client = client
        print("포지션 사이저가 초기화되었습니다.")

    def calculate_position_size(self, symbol: str, entry_price: float, atr: float) -> float:
        """Return the fixed trade size configured for the bot."""

        return config.trade_quantity
