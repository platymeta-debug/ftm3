"""Risk management helper for determining order quantities."""

from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException

from core.config_manager import config


class PositionSizer:
    def __init__(self, client: Client):
        self.client = client
        print("포지션 사이저가 초기화되었습니다.")

    def _get_usdt_balance(self) -> float:
        """바이낸스 선물 계좌의 USDT 잔고를 조회합니다."""
        try:
            account_info = self.client.futures_account_balance()
            for asset in account_info:
                if asset.get("asset") == "USDT":
                    return float(asset.get("balance", 0))
            return 0.0
        except BinanceAPIException as exc:
            print(f"계좌 잔고 조회 실패: {exc}")
            return 0.0
        except Exception as exc:
            print(f"계좌 잔고 조회 중 알 수 없는 오류: {exc}")
            return 0.0

    def _get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            mark_price = self.client.futures_mark_price(symbol=symbol)
            return float(mark_price.get("markPrice"))
        except BinanceAPIException as exc:
            print(f"마크 가격 조회 실패: {exc}")
        except Exception as exc:
            print(f"마크 가격 조회 중 알 수 없는 오류: {exc}")
        return None

    def calculate_position_size(
        self, symbol: str, entry_price: float, atr: float
    ) -> Optional[float]:
        """
        리스크 설정에 기반하여 동적으로 포지션 크기(수량)를 계산합니다.

        :return: 계산된 주문 수량 또는 계산 불가 시 None
        """
        account_balance = self._get_usdt_balance()
        if account_balance <= 0:
            print("계산 불가: 잔고가 0 이하입니다.")
            return None

        if atr is None or atr <= 0:
            print("계산 불가: ATR이 0 이하입니다.")
            return None

        max_risk_per_trade = account_balance * config.risk_target_pct
        stop_loss_distance = atr * config.sl_atr_multiplier

        price = entry_price
        if price is None or price <= 0:
            mark_price = self._get_mark_price(symbol)
            if mark_price is None or mark_price <= 0:
                print(f"가격 조회 실패로 수량 계산 불가: {symbol}")
                return None
            price = mark_price

        if stop_loss_distance <= 0:
            print("계산 불가: 손절매 거리가 0 이하입니다.")
            return None

        quantity = max_risk_per_trade / stop_loss_distance

        if quantity <= 0:
            print("계산 불가: 계산된 수량이 0 이하입니다.")
            return None

        precision = 3
        rounded_quantity = round(quantity, precision)

        if rounded_quantity <= 0:
            print("계산 불가: 반올림된 수량이 0 이하입니다.")
            return None

        print(
            "동적 수량 계산: 잔고=${:,.2f}, 리스크=${:,.2f}, ATR=${:,.2f}, 가격=${:,.2f} -> 수량={}".format(
                account_balance, max_risk_per_trade, atr, price, rounded_quantity
            )
        )
        return rounded_quantity
