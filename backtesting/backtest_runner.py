# backtesting/backtest_runner.py (V22 - 전략 클래스 적용)

import pandas as pd
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
import sys, os, contextlib, io

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.indicator_calculator import calculate_all_indicators
from analysis.data_fetcher import fetch_klines
from core.config_manager import config
# --- ▼▼▼ [핵심] 분리된 전략 부품들을 import 합니다 ▼▼▼ ---
from analysis.strategies.confluence_strategy import ConfluenceStrategy
# --- ▲▲▲ [핵심] ▲▲▲ ---

def prepare_data_for_backtesting(df: pd.DataFrame) -> pd.DataFrame:
    df_renamed = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'})
    return df_renamed

# --- ▼▼▼ [핵심] 백테스팅 전용 '껍데기' 클래스로 변경 ▼▼▼ ---
class StrategyRunner(Strategy):
    # 이 클래스는 이제 어떤 전략이든 실행시켜주는 '실행기' 역할을 합니다.
    # 실제 분석 로직은 모두 외부 전략 클래스에 위임합니다.
    
    # 최적화할 파라미터들을 정의합니다. 이 값들은 전략 클래스로 전달됩니다.
    open_threshold = 12.0
    risk_reward_ratio = 2.5

    def init(self):
        # 실행할 전략 클래스의 인스턴스를 생성합니다.
        # 최적화 과정에서 변경될 파라미터들을 여기에 전달합니다.
        self.strategy = ConfluenceStrategy(open_th=self.open_threshold)
        print(f"'{self.strategy.name}' 전략을 백테스팅합니다.")
        
        # ATR 계산은 여기서 미리 해둡니다.
        self.atr = self.I(lambda x: pd.Series(x).rolling(14).mean(), self.data.df.ta.atr(append=False))

    def next(self):
        # 1. 현재까지의 모든 데이터를 준비합니다.
        df_with_indicators = self.data.df.rename(columns=str.lower)
        df_with_indicators['ATRr_14'] = self.atr
        
        # 2. 외부 전략 클래스의 analyze 메소드를 호출하여 신호를 받습니다.
        analysis_result = self.strategy.analyze(df_with_indicators)
        signal = analysis_result['signal']

        # 3. 신호에 따라 거래를 실행합니다.
        if not self.position:
            entry_price = self.data.Close[-1]
            stop_loss_distance = self.atr[-1] * config.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio

            if signal == 1.0: # 매수 신호
                sl_price = entry_price - stop_loss_distance
                tp_price = entry_price + take_profit_distance
                if sl_price > 0:
                    self.buy(sl=sl_price, tp=tp_price)

            elif signal == -1.0: # 매도 신호
                sl_price = entry_price + stop_loss_distance
                tp_price = entry_price - take_profit_distance
                if tp_price > 0:
                    self.sell(sl=sl_price, tp=tp_price)
# --- ▲▲▲ [핵심] ▲▲▲ ---

if __name__ == '__main__':
    # ... (실행 부분은 거의 동일) ...
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    symbol = "ETHUSDT" # 테스트를 위해 ETH만 실행
    
    print(f"\n{'='*50}\n🚀 {symbol}에 대한 최적화를 시작합니다...\n{'='*50}")
    klines_data = fetch_klines(binance_client, symbol, "1d", limit=500)
    
    if klines_data is not None and not klines_data.empty:
        data_for_bt = prepare_data_for_backtesting(klines_data)
        bt = FractionalBacktest(data_for_bt, StrategyRunner, cash=10_000, commission=.002, finalize_trades=True)
        
        stats = bt.optimize(
            open_threshold=range(4, 13, 2),
            risk_reward_ratio=[1.5, 2.0, 2.5],
            maximize='Equity Final [$]'
        )
        
        print(f"\n--- [{symbol}] 최적화 결과 ---")
        print("\n✅ 가장 성과가 좋았던 파라미터 조합:")
        print(stats._strategy)
        print("\n📊 상세 성과:")
        print(stats)
        
        bt.plot(filename=f"{symbol}_strategy_pattern_result.html")
        print(f"\n📈 {symbol}_strategy_pattern_result.html 파일에 상세 차트가 저장되었습니다.")
