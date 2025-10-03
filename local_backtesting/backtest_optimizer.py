# local_backtesting/backtest_optimizer.py (최종 완성본 - 누락된 인자 전달 오류 해결)

import pandas as pd
import json
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
from collections import deque
import sys
import os
from datetime import timedelta
from tqdm import tqdm

# --- 프로젝트 경로 설정 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from analysis import indicator_calculator, data_fetcher
from analysis.confluence_engine import ConfluenceEngine
from analysis.macro_analyzer import MacroAnalyzer
from core.config_manager import config

def segment_data_by_regime(klines_df: pd.DataFrame, macro_data: dict) -> dict:
    print("\n...과거 데이터 전체에 대한 거시 경제 분석을 시작합니다...")
    macro_analyzer = MacroAnalyzer()
    regime_periods = []
    
    for date in tqdm(klines_df.index, desc="과거 시장 상황 분석 중"):
        # --- ▼▼▼ [수정] 누락되었던 macro_data 인자 전달 ▼▼▼ ---
        regime, _, _ = macro_analyzer.diagnose_macro_regime_for_date(date, macro_data)
        # --- ▲▲▲ [수정] ---
        regime_periods.append(regime.name)
    
    klines_df['Regime'] = regime_periods
    
    segmented_data = {
        "BULL": klines_df[klines_df['Regime'] == 'BULL'],
        "BEAR": klines_df[klines_df['Regime'] == 'BEAR'],
    }
    print("...거시 경제 분석 및 데이터 구간 선별 완료!")
    print(f"   - 강세장(BULL) 데이터: {len(segmented_data['BULL'])}개 캔들")
    print(f"   - 약세장(BEAR) 데이터: {len(segmented_data['BEAR'])}개 캔들")
    return segmented_data

# (OptoRunner 클래스는 이전과 동일)
class OptoRunner(Strategy):
    open_threshold = 12.0
    risk_reward_ratio = 2.5
    sl_atr_multiplier = 1.5
    trend_entry_confirm_count = 3
    symbol = "BTCUSDT"
    market_regime = "BULL"

    def init(self):
        self.engine = ConfluenceEngine(Client("", ""))
        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)
        self.recent_scores = deque(maxlen=int(self.trend_entry_confirm_count))

    def next(self):
        current_index = len(self.data) - 1
        current_data = self.indicators.iloc[:current_index + 1]
        if len(current_data) < self.trend_entry_confirm_count: return
        
        current_score, _ = self.engine._calculate_tactical_score(current_data)
        self.recent_scores.append(current_score)
        if len(self.recent_scores) < self.trend_entry_confirm_count: return
        
        avg_score = sum(self.recent_scores) / len(self.recent_scores)
        
        side = None
        if self.market_regime == "BULL" and avg_score >= self.open_threshold:
            side = "BUY"
        elif self.market_regime == "BEAR" and avg_score <= -self.open_threshold:
            side = "SELL"

        if side and not self.position:
            entry_atr = current_data.iloc[-1].get("ATRr_14", 0)
            if not entry_atr or pd.isna(entry_atr) or entry_atr <= 0: return

            stop_loss_distance = entry_atr * self.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio
            trade_size = 0.50
            
            current_price = self.data.Close[-1]
            sl_price, tp_price = 0, 0

            if side == "BUY":
                sl_price = current_price - stop_loss_distance
                tp_price = current_price + take_profit_distance
            else: # SELL
                sl_price = current_price + stop_loss_distance
                tp_price = current_price - take_profit_distance

            if sl_price <= 0 or tp_price <= 0: return

            if side == "BUY": self.buy(sl=sl_price, tp=tp_price, size=trade_size)
            else: self.sell(sl=sl_price, tp=tp_price, size=trade_size)

if __name__ == '__main__':
    symbols_to_optimize = ["BTCUSDT", "ETHUSDT"]
    initial_cash = 10_000
    binance_client = Client(config.api_key, config.api_secret)
    results_file = os.path.join(project_root, "optimal_settings.json")
    try:
        with open(results_file, 'r', encoding='utf-8') as f: all_settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): all_settings = {}

    macro_analyzer_preload = MacroAnalyzer()
    preloaded_macro_data = macro_analyzer_preload.preload_all_macro_data()

    for symbol in symbols_to_optimize:
        print(f"\n\n{'='*50}\n🚀 **{symbol}** 자동 최적화 시작...\n{'='*50}")
        
        klines_data = data_fetcher.fetch_klines(binance_client, symbol, "1d", limit=1000)
        if klines_data is None or len(klines_data) < 200: continue
        
        segmented_data = segment_data_by_regime(klines_data, preloaded_macro_data)

        for regime in ["BULL", "BEAR"]:
            print(f"\n--- 🔬 [{symbol}] '{regime}' 시장 구간 최적화 ---")
            regime_klines = segmented_data.get(regime)
            if regime_klines is None or len(regime_klines) < 50:
                print(f"'{regime}' 시장 데이터가 부족하여 최적화를 건너뜁니다.")
                continue

            regime_klines.columns = [col.capitalize() for col in regime_klines.columns]
            OptoRunner.symbol = symbol
            OptoRunner.market_regime = regime

            bt = FractionalBacktest(regime_klines, OptoRunner, cash=initial_cash, commission=.002, margin=1/10)
            
            stats = bt.optimize(
                open_threshold=range(10, 20, 2),
                risk_reward_ratio=[2.0, 2.5, 3.0],
                sl_atr_multiplier=[1.0, 1.5, 2.0],
                trend_entry_confirm_count=range(2, 5, 1),
                maximize='Calmar Ratio',
                constraint=lambda p: p.risk_reward_ratio > p.sl_atr_multiplier
            )

            best_params = stats._strategy
            optimized_metric_name = 'Calmar Ratio'
            optimized_metric_value = stats[optimized_metric_name]

            print(f"\n--- ✅ [{symbol}/{regime}] 최적화 완료! ---")
            print(f"   📈 최적화 기준: {optimized_metric_name} = {optimized_metric_value:.4f}")
            print(f"   🎯 찾은 최적 파라미터:")
            print(f"      - OPEN_TH: {best_params.open_threshold}")
            print(f"      - RR_RATIO: {best_params.risk_reward_ratio}")
            print(f"      - SL_ATR_MULTIPLIER: {best_params.sl_atr_multiplier}")
            print(f"      - TREND_ENTRY_CONFIRM_COUNT: {best_params.trend_entry_confirm_count}")

            if regime not in all_settings: all_settings[regime] = {}
            all_settings[regime][symbol] = {
                "OPEN_TH": int(best_params.open_threshold),
                "RR_RATIO": float(best_params.risk_reward_ratio),
                "SL_ATR_MULTIPLIER": float(best_params.sl_atr_multiplier),
                "TREND_ENTRY_CONFIRM_COUNT": int(best_params.trend_entry_confirm_count),
                "OPTIMIZED_METRIC": optimized_metric_name,
                "VALUE": float(round(optimized_metric_value, 4))
            }
            
            with open(results_file, 'w', encoding='utf-8') as f:
                json.dump(all_settings, f, indent=2, ensure_ascii=False)
            print(f"   💾 위 결과를 **{results_file}**에 저장했습니다.")
