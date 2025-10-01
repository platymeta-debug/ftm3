from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException

from core.event_bus import event_bus
from database.manager import db_manager
from database.models import Signal, Trade


class TradingEngine:
    """
    거래 실행의 모든 로직을 담당하는 클래스.
    실제 바이낸스 API와 연동하여 주문을 처리합니다.
    """

    def __init__(self, client: Client):
        self.client = client
        print("트레이딩 엔진이 초기화되었습니다.")

    async def place_order(
        self, symbol: str, side: str, quantity: float, analysis_context: dict
    ) -> None:
        """
        분석 컨텍스트를 기록하고, 실제 바이낸스 주문을 생성한 후, 결과를 처리합니다.
        """
        print(f"주문 실행 요청 수신: {symbol} {side} {quantity}")

        session = db_manager.get_session()
        new_signal: Optional[Signal] = None
        try:
            # 1. 분석 컨텍스트(신호)를 데이터베이스에 기록
            new_signal = Signal(
                symbol=symbol,
                final_score=analysis_context.get("final_score"),
                score_1d=analysis_context.get("tf_scores", {}).get("1d"),
                score_4h=analysis_context.get("tf_scores", {}).get("4h"),
                score_1h=analysis_context.get("tf_scores", {}).get("1h"),
                score_15m=analysis_context.get("tf_scores", {}).get("15m"),
            )
            session.add(new_signal)
            session.commit()  # 신호 ID를 확정하기 위해 먼저 커밋

            if quantity is None or quantity <= 0:
                raise ValueError("주문 수량이 유효하지 않습니다.")

            # 2. 실제 바이낸스 주문 생성
            # newOrderRespType='RESULT'로 설정하여 상세한 체결 정보를 받습니다.
            order_params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": quantity,
                "newOrderRespType": "RESULT",
            }
            binance_order = self.client.futures_create_order(**order_params)

            # 3. 성공한 주문 결과를 DB에 기록
            avg_price = binance_order.get("avgPrice") or binance_order.get("price")
            entry_price = float(avg_price) if avg_price not in (None, "") else 0.0
            new_trade = Trade(
                signal_id=new_signal.id,
                binance_order_id=binance_order.get("orderId"),
                symbol=binance_order.get("symbol"),
                side=binance_order.get("side"),
                quantity=float(binance_order.get("origQty", quantity)),
                entry_price=entry_price,
                status=binance_order.get("status", "FILLED"),
            )
            session.add(new_trade)
            session.commit()

            # 4. 성공 이벤트를 발행
            await event_bus.publish(
                "ORDER_SUCCESS",
                {
                    "symbol": new_trade.symbol,
                    "side": new_trade.side,
                    "quantity": new_trade.quantity,
                    "price": new_trade.entry_price,
                    "source": "ConfluenceEngine",
                    "response": binance_order,
                },
            )

        except BinanceAPIException as exc:
            session.rollback()
            print(f"주문 실패 (API 오류): {exc}")
            if new_signal is not None:
                failed_trade = Trade(
                    signal_id=new_signal.id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    status="REJECTED",
                    pnl=0,
                )
                session.add(failed_trade)
                session.commit()
            await event_bus.publish(
                "ORDER_FAILURE", {"error": str(exc), "source": "TradingEngine"}
            )

        except Exception as exc:
            session.rollback()
            print(f"주문 처리 중 오류 발생: {exc}")
            await event_bus.publish(
                "ORDER_FAILURE", {"error": str(exc), "source": "TradingEngine"}
            )
        finally:
            session.close()
