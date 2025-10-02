from typing import Optional
from binance.client import Client
from binance.exceptions import BinanceAPIException
from core.config_manager import config


class PositionSizer:
    def __init__(self, client: Client):
        self.client = client
        print("포지션 사이저가 초기화되었습니다.")

    def _get_usdt_balance(self) -> float:
        try:
            balances = self.client.futures_account_balance()
            for balance in balances:
                if balance['asset'] == 'USDT':
                    return float(balance['balance'])
            return 0.0
        except BinanceAPIException as e:
            print(f"계좌 잔고 조회 실패: {e}")
            return 0.0

    def get_leverage_for_symbol(self, symbol: str, aggr_level: int) -> int:
        symbol_leverage_map = config.leverage_map.get(symbol, config.leverage_map.get("BTCUSDT"))
        if 1 <= aggr_level <= 4:
            return symbol_leverage_map["LOW"]
        elif 5 <= aggr_level <= 7:
            return symbol_leverage_map["MID"]
        else:
            return symbol_leverage_map["HIGH"]

    def calculate_position_size(
        self, symbol: str, atr: float, aggr_level: int, open_positions_count: int, average_score: float
    ) -> Optional[float]:
        """[V4] 신호 등급과 포트폴리오를 고려하여 동적 포지션 크기를 계산합니다."""
        account_balance = self._get_usdt_balance()
        if account_balance <= 0 or atr <= 0:
            print(f"계산 불가: 잔고({account_balance}) 또는 ATR({atr})이 유효하지 않습니다.")
            return None

        available_slots = config.max_open_positions - open_positions_count
        if available_slots <= 0:
            return None
        capital_to_use = account_balance / available_slots

        # --- ▼▼▼ V4 핵심 로직: 신호 등급별 리스크 스케일 적용 ▼▼▼ ---
        abs_avg_score = abs(average_score)
        if abs_avg_score >= 18.0:
            risk_scale = config.risk_scale_high
            grade = "A"
        elif abs_avg_score >= 15.0:
            risk_scale = config.risk_scale_medium
            grade = "B"
        else:
            risk_scale = config.risk_scale_low
            grade = "C"

        risk_multiplier = 1 + ((aggr_level - 5) / 10.0)
        final_risk_pct = config.risk_target_pct * risk_scale * risk_multiplier
        max_risk_per_trade = capital_to_use * final_risk_pct
        # --- ▲▲▲ V4 핵심 로직 ▲▲▲ ---

        stop_loss_distance = atr * config.sl_atr_multiplier
        if stop_loss_distance <= 0:
            return None
        quantity = max_risk_per_trade / stop_loss_distance

        try:
            info = self.client.futures_exchange_info()
            for s in info['symbols']:
                if s['symbol'] == symbol:
                    precision = s['quantityPrecision']
                    rounded_quantity = round(quantity, precision)
                    if rounded_quantity <= 0:
                        return None
                    print(
                        f"동적 수량 계산(Lvl:{aggr_level}, 등급:{grade}): "
                        f"리스크 ${max_risk_per_trade:,.2f} -> 수량 {rounded_quantity}"
                    )
                    return rounded_quantity
        except Exception as e:
            print(f"수량 정밀도 조회 실패: {e}")
            return None
        return None
