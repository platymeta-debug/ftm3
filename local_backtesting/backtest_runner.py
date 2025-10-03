# local_backtesting/backtest_runner.py (v2.0 - Optimal Settings 기준 실행)

import pandas as pd
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
from collections import deque
import sys
import os
import json

# --- 프로젝트 경로 설정 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from analysis import indicator_calculator
from analysis.confluence_engine import ConfluenceEngine
from analysis.core_strategy import diagnose_market_regime, MarketRegime
from core.config_manager import config
from analysis.data_fetcher import fetch_klines
from local_backtesting.performance_visualizer import create_performance_report

# --- ▼▼▼ [수정] StrategyRunner가 파라미터를 동적으로 받도록 변경 ---
class StrategyRunner(Strategy):
    # 이 값들은 실행 시점에 optimal_settings.json에서 읽어온 값으로 덮어써짐
    open_threshold = 12.0 
    risk_reward_ratio = 2.0
    sl_atr_multiplier = 1.5
    symbol = "BTCUSDT"

    # 고정값
    trend_entry_confirm_count = 3
    
    def init(self):
        self.engine = ConfluenceEngine(Client("", ""))
        if self.data.df.empty: return
        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)
        self.recent_scores = deque(maxlen=self.trend_entry_confirm_count)

    def next(self):
        # ... (기존 OptoRunner와 동일한 매매 로직) ...
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
            'is_above_ema200_1d': last_row.get('close') > last_row.get('EMA_200') if pd.notna(last_row.get('EMA_200')) else False
        })
        market_regime = diagnose_market_regime(market_data_for_diag, config.market_regime_adx_th)
        side = None
        if market_regime == MarketRegime.BULL_TREND and avg_score >= self.open_threshold:
            side = "BUY"
        elif market_regime == MarketRegime.BEAR_TREND and avg_score <= -self.open_threshold:
            side = "SELL"
        if side and not self.position:
            entry_atr = last_row.get("ATRr_14", 0)
            if not entry_atr or pd.isna(entry_atr) or entry_atr <= 0: return
            stop_loss_distance = entry_atr * self.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio
            current_price = self.data.Close[-1]
            sl_price, tp_price = 0, 0
            if side == "BUY":
                sl_price = current_price - stop_loss_distance
                tp_price = current_price + take_profit_distance
            else:
                sl_price = current_price + stop_loss_distance
                tp_price = current_price - take_profit_distance
            if sl_price <= 0 or tp_price <= 0: return
            if side == "BUY": self.buy(sl=sl_price, tp=tp_price, size=0.95)
            else: self.sell(sl=sl_price, tp=tp_price, size=0.95)

if __name__ == '__main__':
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    initial_cash = 10_000
    
    # --- ▼▼▼ [수정] optimal_settings.json을 불러와 기준으로 사용 ---
    optimal_settings_path = os.path.join(project_root, "optimal_settings.json")
    try:
        with open(optimal_settings_path, 'r', encoding='utf-8') as f:
            optimal_settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        optimal_settings = {}
        print("⚠️ optimal_settings.json 파일을 찾을 수 없어 .env 기본값으로 백테스트를 진행합니다.")
    # --- ▲▲▲ [수정] ---

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        print(f"\n🚀 {symbol}에 대한 로컬 백테스팅을 시작합니다...")
        klines_data = fetch_klines(binance_client, symbol, "1d", limit=500)

        if klines_data is not None and not klines_data.empty:
            klines_data.columns = [col.capitalize() for col in klines_data.columns]
            
            # --- ▼▼▼ [수정] optimal_settings에서 파라미터 가져오기 ---
            # 여기서는 'BULL' 시장용 최적값을 사용한다고 가정 (리허설 목적)
            params = optimal_settings.get("BULL", {}).get(symbol)
            if params:
                print(f"✅ optimal_settings.json에서 '{symbol}/BULL' 최적값을 불러와 적용합니다.")
                StrategyRunner.open_threshold = params.get("OPEN_TH", 12.0)
                StrategyRunner.risk_reward_ratio = params.get("RR_RATIO", 2.0)
                StrategyRunner.sl_atr_multiplier = params.get("SL_ATR_MULTIPLIER", 1.5)
            else:
                print(f"⚠️ optimal_settings.json에 '{symbol}/BULL' 최적값이 없습니다. .env 기본값을 사용합니다.")
                # .env 기본값은 config_manager를 통해 간접적으로 적용됨 (StrategyRunner의 기본값)
                default_params = config.get_strategy_params(symbol, "DEFAULT") # FALLBACK
                StrategyRunner.open_threshold = default_params.get("open_th")
                StrategyRunner.risk_reward_ratio = default_params.get("risk_reward_ratio")

            StrategyRunner.symbol = symbol
            print(f"==> '{symbol}' 테스트 파라미터: Threshold={StrategyRunner.open_threshold}, R/R Ratio={StrategyRunner.risk_reward_ratio}, SL Multiplier={StrategyRunner.sl_atr_multiplier} <==")
            # --- ▲▲▲ [수정] ---

            bt = FractionalBacktest(klines_data, StrategyRunner, cash=initial_cash, commission=.002, margin=1/10)
            stats = bt.run()
            
            print(f"\n--- [{symbol}] 백테스팅 결과 ---")
            report_text, chart_buffer = create_performance_report(stats, initial_cash)
            print("\n" + report_text)
            
            results_folder = os.path.join("local_backtesting", "results")
            os.makedirs(results_folder, exist_ok=True)
            chart_filename = os.path.join(results_folder, f"{symbol}_performance_chart.png")
            report_filename = os.path.join(results_folder, f"{symbol}_backtest_report.html")

            if chart_buffer:
                with open(chart_filename, "wb") as f: f.write(chart_buffer.getbuffer())
                print(f"\n📈 {chart_filename} 파일에 상세 차트가 저장되었습니다.")

            bt.plot(filename=report_filename)
            print(f"\n📄 {report_filename} 파일에 상세 리포트가 저장되었습니다.")
