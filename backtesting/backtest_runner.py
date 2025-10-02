# backtesting/backtest_runner.py (V21 - 최종 클린업)

import pandas as pd
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
import sys, os, contextlib, io

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.core_strategy import diagnose_market_regime, MarketRegime
from analysis.indicator_calculator import calculate_all_indicators
from analysis.data_fetcher import fetch_klines
from core.config_manager import config

def prepare_data_for_backtesting(df: pd.DataFrame) -> pd.DataFrame:
    df_renamed = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'})
    return df_renamed

class ConfluenceStrategy(Strategy):
    # 이 값들은 최적화 과정에서 bt.optimize에 의해 동적으로 변경됩니다.
    open_threshold = 4.0 
    risk_reward_ratio = 2.0
    
    # config 파일에서 고정값을 가져옵니다.
    sl_atr_multiplier = config.sl_atr_multiplier
    market_regime_adx_th = config.market_regime_adx_th

    def init(self):
        # 최적화 중에는 출력을 생략하여 진행률 표시줄을 깔끔하게 유지합니다.
        pass

    def next(self):
        df = self.data.df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
        if len(df) < 200: return
        
        # 최적화 중에는 indicator_calculator의 로그를 완전히 숨깁니다.
        with contextlib.redirect_stdout(io.StringIO()):
            df_with_indicators = calculate_all_indicators(df)
            
        if df_with_indicators.empty or 'ATRr_14' not in df_with_indicators.columns: return
        
        last = df_with_indicators.iloc[-1]
        market_data_for_diag = pd.Series({'adx_4h': last.get('ADX_14'), 'is_above_ema200_1d': last.get('close') > last.get('EMA_200')})
        regime = diagnose_market_regime(market_data_for_diag, self.market_regime_adx_th)
        
        total_score = 0
        if regime in [MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND]:
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
        
        entry_price = self.data.Close[-1]
        atr_value = last['ATRr_14']
        if pd.isna(atr_value) or atr_value <= 0: return

        stop_loss_distance = atr_value * self.sl_atr_multiplier
        take_profit_distance = stop_loss_distance * self.risk_reward_ratio

        if final_score > self.open_threshold and not self.position:
            sl_price = entry_price - stop_loss_distance
            tp_price = entry_price + take_profit_distance
            if sl_price <= 0: return
            self.buy(size=0.1, sl=sl_price, tp=tp_price)
        elif final_score < -self.open_threshold and not self.position:
            sl_price = entry_price + stop_loss_distance
            tp_price = entry_price - take_profit_distance
            if tp_price <= 0: return 
            self.sell(size=0.1, sl=sl_price, tp=tp_price)

if __name__ == '__main__':
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    for symbol in config.symbols:
        print(f"\n{'='*50}\n🚀 {symbol}에 대한 최적화를 시작합니다...\n{'='*50}")
        klines_data = fetch_klines(binance_client, symbol, "1d", limit=500)
        if klines_data is not None and not klines_data.empty:
            data_for_bt = prepare_data_for_backtesting(klines_data)
            bt = FractionalBacktest(data_for_bt, ConfluenceStrategy, cash=10_000, commission=.002, finalize_trades=True)
            
            stats = bt.optimize(
                open_threshold=range(4, 13, 2),    # 4, 6, 8, 10, 12를 테스트
                risk_reward_ratio=[1.5, 2.0, 2.5], # 1.5, 2.0, 2.5를 테스트
                maximize='Equity Final [$]',       # 최종 자산이 가장 높은 조합을 찾음
                constraint=lambda p: p.open_threshold > 0
            )
            
            print(f"\n--- [{symbol}] 최적화 결과 ---")
            print("\n✅ 가장 성과가 좋았던 파라미터 조합:")
            print(stats._strategy)
            
            print("\n📊 상세 성과:")
            print(stats)
            
            bt.plot(filename=f"{symbol}_optimization_result.html")
            print(f"\n📈 {symbol}_optimization_result.html 파일에 상세 차트가 저장되었습니다.")
