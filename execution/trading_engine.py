from __future__ import annotations

import time
from binance.client import Client

from core.event_bus import event_bus


class TradingEngine:
    """Skeleton trading engine responsible for handling order requests."""

    def __init__(self, client: Client) -> None:
        self.client = client
        print("트레이딩 엔진이 초기화되었습니다.")

    async def place_order(self, symbol: str, side: str, quantity: float) -> None:
        """Simulate order execution and emit success events."""
        print(f"주문 실행 요청 수신: {symbol} {side} {quantity}")
        # Placeholder for real Binance order logic (Phase 2)

        mock_order_id = int(time.time() * 1000)
        await event_bus.publish(
            "ORDER_SUCCESS",
            {
                "source": "TradingEngine",
                "response": {
                    "symbol": symbol,
                    "side": side,
                    "origQty": quantity,
                    "orderId": mock_order_id,
                },
            },
        )
