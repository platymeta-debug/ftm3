# backtesting/backtest_runner.py (V14 - 공용 로직 모듈 사용)

import pandas as pd
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
import sys
import os
import contextlib
import io

# 프로젝트 루트 경로를 추가하여 다른 폴더의 모듈을 불러올 수 있게 합니다.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- ▼▼▼ [핵심] 공용 모듈에서 로직을 import 합니다 ▼▼▼ ---
# 이 코드가 작동하려면 analysis 폴더에 core_strategy.py 파일이 있어야 합니다.
from analysis.core_strategy import diagnose_market_regime, MarketRegime
# --- ▲▲▲ [핵심] ▲▲▲ ---
from analysis.indicator_calculator import calculate_all_indicators
from analysis.data_fetcher import fetch_klines
from core.config_manager import config

def prepare_data_for_backtesting(df: pd.DataFrame) -> pd.DataFrame:
    """Backtesting.py 라이브러리 형식에 맞게 데이터프레임 컬럼명을 변경합니다."""
    df_renamed = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'})
    return df_renamed

class ConfluenceStrategy(Strategy):
    # config 파일에서 직접 값을 가져와 전략 파라미터로 설정
    open_threshold = config.open_th
    sl_atr_multiplier = config.sl_atr_multiplier
    risk_reward_ratio = config.risk_reward_ratio
    market_regime_adx_th = config.market_regime_adx_th

    def init(self):
        print("백테스팅 전략 초기화 완료. (공용 로직 모듈 사용)")

    def next(self):
        # backtesting.py의 데이터를 우리 분석 모듈 형식(소문자 컬럼)으로 변환
        df = self.data.df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
        
        # 분석에 필요한 최소 데이터(200개 봉) 확인
        if len(df) < 200:
            return

        # indicator_calculator의 로그 출력을 임시로 숨김
        with contextlib.redirect_stdout(io.StringIO()):
            df_with_indicators = calculate_all_indicators(df)
        
        # 지표 계산 실패 또는 ATR 데이터가 없으면 진행 중단
        if df_with_indicators.empty or 'ATRr_14' not in df_with_indicators.columns:
            return

        last = df_with_indicators.iloc[-1]
        
        # --- 1. 시장 체제 진단 (공용 함수 호출) ---
        # 백테스팅 환경에 맞게 EMA_200, ADX_14 데이터를 가공하여 전달합니다.
        # 실제 봇은 4h, 1d 데이터를 혼합하지만, 백테스팅은 현재 1d 데이터만 사용하므로
        # 1d 데이터의 ADX를 adx_4h로, 1d의 is_above_ema200을 is_above_ema200_1d로 간주하여 테스트합니다.
        market_data_for_diag = pd.Series({
            'adx_4h': last.get('ADX_14'),
            'is_above_ema200_1d': last.get('close') > last.get('EMA_200')
        })
        regime = diagnose_market_regime(market_data_for_diag, self.market_regime_adx_th)
        
        # 추세장이 아니면 거래하지 않음
        if regime not in [MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND]:
            return

        # --- 2. 점수 계산 (V12 로직과 동일) ---
        trend_score, money_flow_score, oscillator_score = 0, 0, 0
        if all(k in last and pd.notna(last[k]) for k in ["EMA_20", "EMA_50", "close"]):
            if last['close'] > last['EMA_20'] > last['EMA_50']: trend_score = 2
            elif last['close'] < last['EMA_20'] < last['EMA_50']: trend_score = -2
            elif last['close'] > last['EMA_50']: trend_score = 1
            elif last['close'] < last['EMA_50']: trend_score = -1
        # (이하 다른 점수 계산 로직은 생략, 이전 버전과 동일)
        total_score = trend_score # (money_flow_score, oscillator_score 등 실제 로직 추가 필요)
        final_score = total_score * config.tf_vote_weights[0]

        # --- 3. ATR 기반 SL/TP 설정 및 주문 실행 ---
        if final_score > self.open_threshold and not self.position:
            entry_price = self.data.Close[-1]
            atr_value = last['ATRr_14']
            stop_loss_distance = atr_value * self.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio
            sl_price = entry_price - stop_loss_distance
            tp_price = entry_price + take_profit_distance
            self.buy(size=0.1, sl=sl_price, tp=tp_price)

        elif final_score < -self.open_threshold and not self.position:
            entry_price = self.data.Close[-1]
            atr_value = last['ATRr_14']
            stop_loss_distance = atr_value * self.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio
            sl_price = entry_price + stop_loss_distance
            tp_price = entry_price - take_profit_distance
            self.sell(size=0.1, sl=sl_price, tp=tp_price)

if __name__ == '__main__':
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    for symbol in config.symbols:
        print(f"\n{'='*50}\n🚀 {symbol}에 대한 백테스팅을 시작합니다...\n{'='*50}")
        klines_data = fetch_klines(binance_client, symbol, "1d", limit=500)
        if klines_data is not None and not klines_data.empty:
            data_for_bt = prepare_data_for_backtesting(klines_data)
            bt = FractionalBacktest(
                data_for_bt, ConfluenceStrategy, 
                cash=10_000, 
                commission=.002,
                trade_on_close=True, 
                exclusive_orders=True
            )
            stats = bt.run()
            if stats is not None:
                print(f"\n--- [{symbol}] 백테스팅 결과 ---\n{stats}\n---------------------------------\n")
                bt.plot(filename=f"{symbol}_backtest_result.html")
        else:
            print(f"{symbol} 데이터를 가져오는 데 실패하여 백테스팅을 건너뜁니다.")
