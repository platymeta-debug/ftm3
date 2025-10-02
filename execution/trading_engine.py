from typing import Optional
from binance.client import Client
from binance.exceptions import BinanceAPIException
from database.manager import db_manager, Signal, Trade

class TradingEngine:
    def __init__(self, client: Client):
        self.client = client
        print("트레이딩 엔진이 초기화되었습니다.")

    async def place_order(
        self, symbol: str, side: str, quantity: float, analysis_context: dict
    ) -> None:
        session = db_manager.get_session()
        try:
            signal_id = analysis_context.get("signal_id")
            linked_signal: Optional[Signal] = None
            if signal_id:
                linked_signal = session.get(Signal, signal_id)
                if linked_signal is None:
                    print(f"⚠️ 연관 신호(ID={signal_id})를 찾을 수 없어 새 거래와 연결하지 않습니다.")

            leverage = analysis_context.get("leverage")
            if leverage:
                try:
                    print(f"레버리지 설정 시도: {symbol} {leverage}x")
                    self.client.futures_change_leverage(symbol=symbol, leverage=int(leverage))
                except BinanceAPIException as leverage_err:
                    print(f"레버리지 설정 실패: {leverage_err}")

            # 주문 실행
            order_params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity, "newOrderRespType": "RESULT"}
            binance_order = self.client.futures_create_order(**order_params)

            # 거래 DB 기록 (entry_atr 및 최고가 초기화)
            avg_price = binance_order.get("avgPrice") or binance_order.get("price")
            entry_price = float(avg_price) if avg_price not in (None, "") else 0.0
            trade_qty = binance_order.get("origQty", quantity)
            linked_signal_id = linked_signal.id if linked_signal else None
            new_trade = Trade(
                signal_id=linked_signal_id,
                binance_order_id=binance_order.get("orderId"),
                symbol=symbol,
                side=side,
                quantity=float(trade_qty),
                entry_price=entry_price,
                status="OPEN",
                entry_atr=analysis_context.get("entry_atr"),
                highest_price_since_entry=entry_price
            )
            session.add(new_trade)
            session.commit()
            print(f"✅ 주문 성공 및 DB 기록 완료: {symbol} {side} {quantity}")

            # ... (이벤트 발행은 기존과 동일)

        except Exception as exc:
            session.rollback()
            print(f"🚨 주문 처리 중 오류 발생: {exc}")
            # ... (실패 이벤트 발행은 기존과 동일)
        finally:
            session.close()

    async def close_position(self, trade_to_close: Trade, reason: str) -> None:
        # 이 함수는 이전 버전과 거의 동일하게 유지
        pass
