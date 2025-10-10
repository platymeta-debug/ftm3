# risk_management/position_sizer.py (V8 업그레이드)

from typing import Optional
from binance.client import Client
from binance.exceptions import BinanceAPIException
from core.config_manager import ConfigManager


class PositionSizer:
    def __init__(self, client: Client, config: ConfigManager):
        self.client = client
        self.config = config
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
        # 이 부분은 config에 leverage_map이 정의되어 있다는 가정 하에 동작합니다.
        # 해당 설정이 없다면 기본값을 사용하도록 수정이 필요할 수 있습니다.
        default_map = {"LOW": 5, "MID": 10, "HIGH": 15}
        symbol_leverage_map = getattr(self.config, 'leverage_map', {}).get(symbol, default_map)
        
        if 1 <= aggr_level <= 4: return symbol_leverage_map["LOW"]
        elif 5 <= aggr_level <= 7: return symbol_leverage_map["MID"]
        else: return symbol_leverage_map["HIGH"]

    def calculate_position_size(
        self, symbol: str, atr: float, aggr_level: int, open_positions_count: int, 
        average_score: float, market_regime: str
    ) -> Optional[float]:
        """
        V8: 시장 국면(market_regime)을 인자로 받아 해당 국면에 최적화된 리스크 설정을 사용합니다.
        """
        account_balance = self._get_usdt_balance()
        if account_balance <= 0 or atr <= 0:
            print(f"계산 불가: 잔고({account_balance}) 또는 ATR({atr})이 유효하지 않습니다.")
            return None

        available_slots = self.config.max_open_positions - open_positions_count
        if available_slots <= 0:
            return None
        capital_to_use = account_balance / available_slots

        # --- ▼▼▼ V8 핵심 로직: 시장 국면별 리스크 파라미터 동적 로드 ▼▼▼ ---
        exec_policy = self.config.get_exec_policy(symbol, market_regime)
        risk_per_trade = exec_policy.get('risk_per_trade', 0.01) # 국면에 맞는 리스크 설정
        sl_atr_multiplier = exec_policy.get('sl_atr_multiplier', 1.5) # 국면에 맞는 SL 배수
        # --- ▲▲▲ V8 핵심 로직 ▲▲▲ ---

        # 신호 등급별 리스크 스케일 적용 (기존 로직 유지)
        abs_avg_score = abs(average_score)
        risk_scale_high = getattr(self.config, 'risk_scale_high', 1.5)
        risk_scale_medium = getattr(self.config, 'risk_scale_medium', 1.0)
        risk_scale_low = getattr(self.config, 'risk_scale_low', 0.5)

        if abs_avg_score >= 18.0: risk_scale, grade = risk_scale_high, "A"
        elif abs_avg_score >= 15.0: risk_scale, grade = risk_scale_medium, "B"
        else: risk_scale, grade = risk_scale_low, "C"

        risk_multiplier = 1 + ((aggr_level - 5) / 10.0)
        final_risk_pct = risk_per_trade * risk_scale * risk_multiplier
        max_risk_per_trade = capital_to_use * final_risk_pct

        stop_loss_distance = atr * sl_atr_multiplier
        if stop_loss_distance <= 0:
            return None
        quantity = max_risk_per_trade / stop_loss_distance

        try:
            info = self.client.futures_exchange_info()
            for s in info['symbols']:
                if s['symbol'] == symbol:
                    precision = s['quantityPrecision']
                    rounded_quantity = round(quantity, precision)
                    if rounded_quantity <= 0: return None
                    print(
                        f"동적 수량 계산(Lvl:{aggr_level}, 등급:{grade}, 국면:{market_regime}): "
                        f"리스크 ${max_risk_per_trade:,.2f} -> 수량 {rounded_quantity}"
                    )
                    return rounded_quantity
        except Exception as e:
            print(f"수량 정밀도 조회 실패: {e}")
            return None
        return None
