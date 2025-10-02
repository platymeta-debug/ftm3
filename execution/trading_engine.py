from typing import Optional
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from core.event_bus import event_bus
from database.manager import db_manager, Signal, Trade

class TradingEngine:
    def __init__(self, client: Client):
        self.client = client
        print("트레이딩 엔진이 초기화되었습니다.")

    async def place_order(
        self, symbol: str, side: str, quantity: float, leverage: int, atr: float, analysis_context: dict
    ) -> None:
        session = db_manager.get_session()
        new_signal: Optional[Signal] = None
        try:
            # 1. 신호 DB 기록 (기존과 동일)
            new_signal = Signal(**analysis_context) # analysis_context에 모든 정보 담기
            session.add(new_signal)
            session.commit()

            # 2. 레버리지 설정
            print(f"레버리지 설정 시도: {symbol} {leverage}x")
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)

            # 3. 주문 실행
            order_params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity, "newOrderRespType": "RESULT"}
            binance_order = self.client.futures_create_order(**order_params)

            # 4. 거래 DB 기록 (entry_atr 추가)
            entry_price = float(binance_order.get("avgPrice", 0.0))
            new_trade = Trade(
                signal_id=new_signal.id,
                binance_order_id=binance_order.get("orderId"),
                symbol=symbol, side=side, quantity=quantity,
                entry_price=entry_price,
                status="OPEN",
                entry_atr=atr # 진입 시점 ATR 기록
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
