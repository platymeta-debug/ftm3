# local_backtesting/backtest_runner.py (FractionalBacktest 적용 최종 완성본)

import pandas as pd
# ▼▼▼ [핵심 수정] FractionalBacktest를 임포트합니다. ▼▼▼
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
# ▲▲▲ [핵심 수정] ▲▲▲
from binance.client import Client
from collections import deque
import sys
import os

# --- (프로젝트 경로 설정 및 다른 임포트는 동일) ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from analysis import indicator_calculator
from analysis.confluence_engine import ConfluenceEngine
from analysis.core_strategy import diagnose_market_regime, MarketRegime
from core.config_manager import config
from analysis.data_fetcher import fetch_klines
from local_backtesting.performance_visualizer import create_performance_report


# --- (StrategyRunner 클래스는 이전과 동일) ---
class StrategyRunner(Strategy):
    open_threshold = 12.0
    risk_reward_ratio = 2.5
    trend_entry_confirm_count = 3
    symbol = "BTCUSDT"

    def init(self):
        print("[StrategyRunner] init() 메소드 시작.")
        self.engine = ConfluenceEngine(Client("", ""))
        if self.data.df.empty: return
        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)
        self.recent_scores = deque(maxlen=self.trend_entry_confirm_count)
        print(f"[StrategyRunner] init() 완료. (총 {len(self.indicators)}개 데이터)")

    def next(self):
        current_index = len(self.data) - 1
        current_data = self.indicators.iloc[:current_index + 1]
        if len(current_data) < self.trend_entry_confirm_count: return
        
        current_score, _ = self.engine._calculate_tactical_score(current_data)
        self.recent_scores.append(current_score)
        if len(self.recent_scores) < self.trend_entry_confirm_count: return
        avg_score = sum(self.recent_scores) / len(self.recent_scores)
        
        last_row = current_data.iloc[-1]
        market_data_for_diag = pd.Series({
            'adx_4h': last_row.get('ADX_14'), 
            'is_above_ema200_1d': last_row.get('close') > last_row.get('EMA_200')
        })
        market_regime = diagnose_market_regime(market_data_for_diag, config.market_regime_adx_th)
        
        side = None
        if market_regime == MarketRegime.BULL_TREND and avg_score >= self.open_threshold:
            side = "BUY"
        elif market_regime == MarketRegime.BEAR_TREND and avg_score <= -self.open_threshold:
            side = "SELL"

        if side and not self.position:
            print(f"✅ Day {current_index}: [{self.symbol}] {side} 진입! (Avg Score: {avg_score:.2f}, Regime: {market_regime.value})")
            
            entry_atr = last_row.get("ATRr_14", 0)
            if not entry_atr or pd.isna(entry_atr) or entry_atr <= 0: return

            stop_loss_distance = entry_atr * config.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio
            trade_size = 0.95

            if side == "BUY":
                self.buy(sl=self.data.Close[-1] - stop_loss_distance,
                         tp=self.data.Close[-1] + take_profit_distance,
                         size=trade_size)
            elif side == "SELL":
                self.sell(sl=self.data.Close[-1] + stop_loss_distance,
                          tp=self.data.Close[-1] - take_profit_distance,
                          size=trade_size)

if __name__ == '__main__':
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    
    try:
        account_info = binance_client.futures_account()
        initial_cash = float(account_info.get('totalWalletBalance', 10000))
        print(f"💰 실제 계좌 잔고를 시작 자본금으로 설정: ${initial_cash:,.2f}")
    except Exception as e:
        initial_cash = 10_000
        print(f"⚠️ 계좌 정보 조회 실패: {e}. 기본 자본금($10,000)으로 시작합니다.")

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        print(f"\n🚀 {symbol}에 대한 로컬 백테스팅을 시작합니다...")
        klines_data = fetch_klines(binance_client, symbol, "1d", limit=500)

        if klines_data is not None and not klines_data.empty:
            klines_data.columns = [col.capitalize() for col in klines_data.columns]
            
            strategy_params = config.get_strategy_params(symbol)
            StrategyRunner.open_threshold = strategy_params.get("open_th")
            StrategyRunner.risk_reward_ratio = strategy_params.get("risk_reward_ratio")
            StrategyRunner.symbol = symbol
            print(f"==> '{symbol}' 테스트 파라미터: Threshold={StrategyRunner.open_threshold}, R/R Ratio={StrategyRunner.risk_reward_ratio} <==")

            # ▼▼▼ [핵심 수정] Backtest를 FractionalBacktest로 교체합니다. ▼▼▼
            bt = FractionalBacktest(klines_data, StrategyRunner, cash=initial_cash, commission=.002, margin=1/10)
            stats = bt.run()
            
            print(f"\n--- [{symbol}] 백테스팅 결과 ---")
            report_text, chart_buffer = create_performance_report(stats, initial_cash)
            print("\n" + report_text)
            
            # ... (이하 파일 저장 로직은 동일) ...
            results_folder = os.path.join("local_backtesting", "results")
            os.makedirs(results_folder, exist_ok=True)
            chart_filename = os.path.join(results_folder, f"{symbol}_performance_chart.png")
            report_filename = os.path.join(results_folder, f"{symbol}_backtest_report.html")

            if chart_buffer:
                with open(chart_filename, "wb") as f:
                    f.write(chart_buffer.getbuffer())
                print(f"\n📈 {chart_filename} 파일에 상세 차트가 저장되었습니다.")

            bt.plot(filename=report_filename)
            print(f"\n📄 {report_filename} 파일에 상세 리포트가 저장되었습니다.")
