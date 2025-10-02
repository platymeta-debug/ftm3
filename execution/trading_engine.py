from typing import Optional
from binance.client import Client
from binance.exceptions import BinanceAPIException
# --- ▼▼▼ 수정된 부분 ▼▼▼ ---
from database.manager import db_manager
from database.models import Signal, Trade # Signal과 Trade를 models.py에서 가져오도록 수정
# --- ▲▲▲ 수정된 부분 ▲▲▲ ---

class TradingEngine:
    def __init__(self, client: Client):
        self.client = client
        print("트레이딩 엔진이 초기화되었습니다.")

    async def place_order_with_bracket(
        self, symbol: str, side: str, quantity: float, leverage: int, entry_atr: float, analysis_context: dict
    ) -> None:
        """[V4] 시장가 진입과 함께 손절/익절 가격을 DB에 기록하는 브라켓 주문을 실행합니다."""
        session = db_manager.get_session()
        try:
            print(f"레버리지 설정 시도: {symbol} {leverage}x")
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)

            order_params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity}
            binance_order = self.client.futures_create_order(**order_params)
            entry_price = float(binance_order.get('avgPrice', 0.0))
            if entry_price == 0.0:
                entry_price = float(binance_order.get('price', 0.0))

            stop_loss_distance = entry_atr * config.sl_atr_multiplier
            if side == "BUY":
                stop_loss_price = entry_price - stop_loss_distance
                take_profit_price = entry_price + (stop_loss_distance * config.risk_reward_ratio)
            else:
                stop_loss_price = entry_price + stop_loss_distance
                take_profit_price = entry_price - (stop_loss_distance * config.risk_reward_ratio)

            new_trade = Trade(
                signal_id=analysis_context.get("signal_id"),
                binance_order_id=binance_order.get("orderId"),
                symbol=symbol,
                side=side,
                quantity=float(binance_order.get('origQty', quantity)),
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
            print(
                f"   ㄴ SL: ${stop_loss_price:,.2f}, TP: ${take_profit_price:,.2f} "
                f"(손익비 1:{config.risk_reward_ratio})"
            )

        except Exception as e:
            session.rollback()
            print(f"🚨 주문 처리 중 오류 발생: {e}")
        finally:
            session.close()

    async def close_position(self, trade_to_close: Trade, reason: str) -> None:
        """[V4] 지정된 거래(포지션)를 시장가로 청산하고 DB를 업데이트합니다."""
        session = db_manager.get_session()
        try:
            close_side = "BUY" if trade_to_close.side == "SELL" else "SELL"

            position_info = self.client.futures_position_information(symbol=trade_to_close.symbol)
            quantity_to_close = abs(float(position_info[0]['positionAmt']))

            if quantity_to_close == 0:
                print(f"⚠️ 청산할 포지션이 이미 없습니다: {trade_to_close.symbol}. DB 상태를 'CLOSED'로 강제 업데이트합니다.")
                trade_to_close.status = "CLOSED"
                session.commit()
                return

            print(f"포지션 종료 요청: {trade_to_close.symbol} {close_side} {quantity_to_close} | 사유: {reason}")

            close_order = self.client.futures_create_order(
                symbol=trade_to_close.symbol, side=close_side, type='MARKET', quantity=quantity_to_close
            )

            exit_price = float(close_order.get("avgPrice", 0.0))
            pnl = (
                (exit_price - trade_to_close.entry_price) * trade_to_close.quantity
                if trade_to_close.side == "BUY"
                else (trade_to_close.entry_price - exit_price) * trade_to_close.quantity
            )

            trade = session.get(Trade, trade_to_close.id)
            trade.status = "CLOSED"
            trade.exit_price = exit_price
            trade.exit_time = datetime.utcnow()
            trade.pnl = pnl
            session.commit()
            print(f"✅ 포지션 종료 및 DB 업데이트 완료. PnL: ${pnl:,.2f}")

        except Exception as e:
            session.rollback()
            print(f"🚨 포지션 종료 처리 중 오류: {e}")
        finally:
            session.close()
