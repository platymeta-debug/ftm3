# backtesting/backtest_runner.py (V10 - 소수점 거래 최종 해결)

import pandas as pd
# --- ▼▼▼ [핵심 수정] FractionalBacktest를 import 합니다 ▼▼▼ ---
from backtesting import Strategy
from backtesting.lib import FractionalBacktest 
# --- ▲▲▲ [핵심 수정] FractionalBacktest를 import 합니다 ▲▲▲ ---
from binance.client import Client
import sys
import os
import contextlib
import io

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.indicator_calculator import calculate_all_indicators
from analysis.data_fetcher import fetch_klines
from core.config_manager import config

def prepare_data_for_backtesting(df: pd.DataFrame) -> pd.DataFrame:
    df_renamed = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'})
    return df_renamed

class ConfluenceStrategy(Strategy):
    open_threshold = 8.0

    def init(self):
        print("ConfluenceEngine 백테스팅 전략 초기화 완료.")

    def next(self):
        df = self.data.df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
        if len(df) < 200: return

        with contextlib.redirect_stdout(io.StringIO()):
            df_with_indicators = calculate_all_indicators(df)
        
        if df_with_indicators.empty: return

        last = df_with_indicators.iloc[-1]
        trend_score, money_flow_score, oscillator_score = 0, 0, 0

        if all(k in last and pd.notna(last[k]) for k in ["EMA_20", "EMA_50", "close"]):
            if last['close'] > last['EMA_20'] > last['EMA_50']: trend_score = 2
            elif last['close'] < last['EMA_20'] < last['EMA_50']: trend_score = -2
            elif last['close'] > last['EMA_50']: trend_score = 1
            elif last['close'] < last['EMA_50']: trend_score = -1

        if all(k in last and pd.notna(last[k]) for k in ["MFI_14", "OBV"]):
            obv_ema = df_with_indicators['OBV'].ewm(span=20, adjust=False).mean().iloc[-1]
            if last['MFI_14'] > 80: money_flow_score -= 1
            if last['MFI_14'] < 20: money_flow_score += 1
            if last['OBV'] > obv_ema: money_flow_score += 1
            if last['OBV'] < obv_ema: money_flow_score -= 1

        if all(k in last and pd.notna(last[k]) for k in ["RSI_14", "STOCHk_14_3_3"]):
            if last['RSI_14'] < 30 and last['STOCHk_14_3_3'] < 20: oscillator_score = 2
            elif last['RSI_14'] > 70 and last['STOCHk_14_3_3'] > 80: oscillator_score = -2
            elif last['RSI_14'] < 40: oscillator_score = 1
            elif last['RSI_14'] > 60: oscillator_score = -1
        
        total_score = trend_score + money_flow_score + oscillator_score
        final_score = total_score * config.tf_vote_weights[0]
        
        if final_score > self.open_threshold and not self.position:
            self.buy(size=0.1)
        elif final_score < -self.open_threshold and not self.position:
            self.sell(size=0.1)
        elif self.position.is_long and final_score < 0:
            self.position.close()
        elif self.position.is_short and final_score > 0:
            self.position.close()

if __name__ == '__main__':
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    for symbol in config.symbols:
        print(f"\n{'='*50}\n🚀 {symbol}에 대한 백테스팅을 시작합니다...\n{'='*50}")
        klines_data = fetch_klines(binance_client, symbol, "1d", limit=500)
        if klines_data is not None and not klines_data.empty:
            data_for_bt = prepare_data_for_backtesting(klines_data)
            
            # --- ▼▼▼ [핵심 수정] Backtest 대신 FractionalBacktest를 사용합니다 ▼▼▼ ---
            bt = FractionalBacktest(
                data_for_bt, ConfluenceStrategy, 
                cash=10_000, 
                commission=.002,
                trade_on_close=True, exclusive_orders=True
            )
            # --- ▲▲▲ [핵심 수정] Backtest 대신 FractionalBacktest를 사용합니다 ▲▲▲ ---
            
            stats = bt.run()
            
            if stats is not None:
                print(f"\n--- [{symbol}] 백테스팅 결과 ---\n{stats}\n---------------------------------\n")
                bt.plot(filename=f"{symbol}_backtest_result.html")
        else:
            print(f"{symbol} 데이터를 가져오는 데 실패하여 백테스팅을 건너뜁니다.")
