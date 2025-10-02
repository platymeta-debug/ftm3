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
        else: # 8 to 10
            return symbol_leverage_map["HIGH"]

    def calculate_position_size(self, symbol: str, aggr_level: int, atr: float) -> Optional[float]:
        account_balance = self._get_usdt_balance()
        if account_balance <= 0 or atr <= 0:
            print(f"계산 불가: 잔고({account_balance}) 또는 ATR({atr})이 유효하지 않습니다.")
            return None

        risk_multiplier = 1 + ((aggr_level - 5) / 10.0) # Level 5=1.0x, 1=0.6x, 10=1.5x
        dynamic_risk_pct = config.risk_target_pct * risk_multiplier
        max_risk_per_trade = account_balance * dynamic_risk_pct
        
        stop_loss_distance = atr * config.sl_atr_multiplier
        if stop_loss_distance <= 0:
            print("계산 불가: 손절 거리가 0 이하입니다.")
            return None
            
        quantity = max_risk_per_trade / stop_loss_distance

        try:
            info = self.client.futures_exchange_info()
            for s in info['symbols']:
                if s['symbol'] == symbol:
                    precision = s['quantityPrecision']
                    rounded_quantity = round(quantity, precision)
                    if rounded_quantity <= 0:
                        print("계산 불가: 반올림된 수량이 0 이하입니다.")
                        return None
                    print(f"동적 수량 계산(Lvl:{aggr_level}): 리스크=${max_risk_per_trade:,.2f} -> 수량={rounded_quantity}")
                    return rounded_quantity
        except Exception as e:
            print(f"수량 정밀도 조회 실패: {e}")
            return None
        
        return None
