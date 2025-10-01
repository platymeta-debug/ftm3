from __future__ import annotations

from binance.client import Client

from core.event_bus import event_bus
from database.manager import db_manager
from database.models import Signal, Trade


class TradingEngine:
    """Skeleton trading engine responsible for handling order requests."""

    def __init__(self, client: Client) -> None:
        self.client = client
        print("트레이딩 엔진이 초기화되었습니다.")

    async def place_order(
        self, symbol: str, side: str, quantity: float, analysis_context: dict
    ) -> None:
        """Simulate order execution and emit success events."""
        print(f"주문 실행 요청 수신: {symbol} {side} {quantity}")

        session = db_manager.get_session()
        try:
            new_signal = Signal(
                symbol=symbol,
                final_score=analysis_context.get("final_score"),
                score_1d=analysis_context.get("tf_scores", {}).get("1d"),
                score_4h=analysis_context.get("tf_scores", {}).get("4h"),
                score_1h=analysis_context.get("tf_scores", {}).get("1h"),
                score_15m=analysis_context.get("tf_scores", {}).get("15m"),
            )
            session.add(new_signal)
            session.commit()

            entry_price = 66000.0

            new_trade = Trade(
                signal_id=new_signal.id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                status="OPEN",
            )
            session.add(new_trade)
            session.commit()

            await event_bus.publish(
                "ORDER_SUCCESS",
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": entry_price,
                    "source": "ConfluenceEngine",
                },
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            session.rollback()
            print(f"주문 처리 중 DB 오류 발생: {exc}")
            await event_bus.publish(
                "ORDER_FAILURE",
                {"error": str(exc), "source": "TradingEngine"},
            )
        finally:
            session.close()
