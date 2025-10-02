# 파일명: execution/trading_engine.py (V4 업그레이드)

from typing import Optional
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from core.config_manager import config
from core.event_bus import event_bus
from database.manager import db_manager
from database.models import Signal, Trade

class TradingEngine:
    def __init__(self, client: Client):
        self.client = client
        print("🚚 [V4] 트레이딩 엔진이 초기화되었습니다.")

    # ... place_order_with_bracket 함수는 V3와 동일 ...
    async def place_order_with_bracket(
        self, symbol: str, side: str, quantity: float, leverage: int, entry_atr: float, analysis_context: dict
    ) -> None:
        """[V4] 시장가 진입과 함께 손절/익절 가격을 DB에 기록하는 브라켓 주문을 실행합니다."""
        session = db_manager.get_session()
        try:
            # 1. 레버리지 설정
            print(f"레버리지 설정 시도: {symbol} {leverage}x")
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)

            # 2. 시장가 주문 실행
            order_params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity, "newOrderRespType": "RESULT"}
            binance_order = self.client.futures_create_order(**order_params)
            entry_price = float(binance_order.get('avgPrice', 0.0))
            if entry_price == 0.0:
                entry_price = float(binance_order.get('price', 0.0))

            # 3. 손절/익절 가격 계산
            stop_loss_distance = entry_atr * config.sl_atr_multiplier
            if side == "BUY":
                stop_loss_price = entry_price - stop_loss_distance
                take_profit_price = entry_price + (stop_loss_distance * config.risk_reward_ratio)
            else: # SELL
                stop_loss_price = entry_price + stop_loss_distance
                take_profit_price = entry_price - (stop_loss_distance * config.risk_reward_ratio)

            # 4. DB에 거래 정보 및 브라켓 가격 기록
            new_trade = Trade(
                signal_id=analysis_context.get("signal_id"),
                binance_order_id=binance_order.get("orderId"),
                symbol=symbol, side=side, quantity=float(binance_order.get('origQty', quantity)),
                entry_price=entry_price,
                entry_atr=entry_atr,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                highest_price_since_entry=entry_price,
                status="OPEN"
            )
            session.add(new_trade)
            session.commit()
            print(f"✅ 주문 성공 및 DB 기록 완료: {symbol} {side} {quantity}")
            print(f"   ㄴ SL: ${stop_loss_price:,.2f}, TP: ${take_profit_price:,.2f} (손익비 1:{config.risk_reward_ratio})")
            
            # 이벤트 발행
            await event_bus.publish("ORDER_SUCCESS", {"trade": new_trade, "context": analysis_context})

        except Exception as e:
            session.rollback()
            print(f"🚨 주문 처리 중 오류 발생: {e}")
            await event_bus.publish("ORDER_FAILURE", {"error": str(e)})
        finally:
            session.close()


    async def close_position(self, trade_to_close: Trade, reason: str, quantity_to_close: Optional[float] = None) -> None:
        """
        [V4] 지정된 거래(포지션)를 시장가로 청산하고 DB를 업데이트합니다.
        quantity_to_close가 지정되면 부분 청산을, None이면 전체 청산을 실행합니다.
        """
        session = db_manager.get_session()
        try:
            trade = session.get(Trade, trade_to_close.id)
            if not trade or trade.status == "CLOSED":
                print(f"이미 처리되었거나 존재하지 않는 거래입니다: ID {trade_to_close.id}")
                return

            close_side = "BUY" if trade.side == "SELL" else "SELL"
            
            # 청산할 수량 결정
            if quantity_to_close is None: # 전체 청산
                position_info = self.client.futures_position_information(symbol=trade.symbol)
                current_position_amt = abs(float(position_info[0]['positionAmt']))
                if current_position_amt == 0:
                    print(f"⚠️ 청산할 포지션이 이미 없습니다: {trade.symbol}. DB 상태를 'CLOSED'로 강제 업데이트합니다.")
                    trade.status = "CLOSED"
                    session.commit()
                    return
                quantity_to_close = current_position_amt
            
            print(f"포지션 종료 요청: {trade.symbol} {close_side} {quantity_to_close} | 사유: {reason}")
            
            close_order = self.client.futures_create_order(
                symbol=trade.symbol, side=close_side, type='MARKET', quantity=quantity_to_close, newOrderRespType="RESULT"
            )
            
            exit_price = float(close_order.get("avgPrice", 0.0))
            pnl = (exit_price - trade.entry_price) * quantity_to_close if trade.side == "BUY" else (trade.entry_price - exit_price) * quantity_to_close
            
            # 남은 포지션이 있는지 확인
            position_info = self.client.futures_position_information(symbol=trade.symbol)
            remaining_amt = abs(float(position_info[0]['positionAmt']))

            if remaining_amt > 0: # 부분 청산 완료
                trade.pnl = (trade.pnl or 0) + pnl
                trade.quantity -= quantity_to_close # 남은 수량 업데이트
                print(f"💰 부분 익절 완료. PnL: ${pnl:,.2f} | 남은 수량: {trade.quantity}")
                # is_scaled_out 플래그는 trading_decision_loop에서 직접 처리
            else: # 전체 청산 완료
                trade.status = "CLOSED"
                trade.exit_price = exit_price
                trade.exit_time = datetime.utcnow()
                trade.pnl = (trade.pnl or 0) + pnl
                print(f"✅ 포지션 전체 종료 및 DB 업데이트 완료. 최종 PnL: ${trade.pnl:,.2f}")

            session.commit()
            await event_bus.publish("ORDER_CLOSE_SUCCESS", {"trade": trade, "reason": reason, "is_partial": remaining_amt > 0})

        except Exception as e:
            session.rollback()
            print(f"🚨 포지션 종료 처리 중 오류: {e}")
        finally:
            session.close()
