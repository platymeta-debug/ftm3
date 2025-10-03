# local_backtesting/backtest_optimizer.py (경로 문제 해결 최종본)

import pandas as pd
import json
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
from collections import deque
import sys
import os

# --- 프로젝트 경로 설정 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from analysis import indicator_calculator
from analysis.confluence_engine import ConfluenceEngine
from analysis.core_strategy import diagnose_market_regime, MarketRegime
from core.config_manager import config
from analysis.data_fetcher import fetch_klines

# OptoRunner 클래스는 이전과 동일합니다.
class OptoRunner(Strategy):
    open_threshold = 12.0
    risk_reward_ratio = 2.5
    sl_atr_multiplier = 1.5
    trend_entry_confirm_count = 3
    symbol = "BTCUSDT"

    def init(self):
        self.engine = ConfluenceEngine(Client("", ""))
        if self.data.df.empty: return
        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)
        self.recent_scores = deque(maxlen=self.trend_entry_confirm_count)

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
            entry_atr = last_row.get("ATRr_14", 0)
            if not entry_atr or pd.isna(entry_atr) or entry_atr <= 0: return
            stop_loss_distance = entry_atr * self.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio
            trade_size = 0.95
            if side == "BUY":
                self.buy(sl=self.data.Close[-1] - stop_loss_distance, tp=self.data.Close[-1] + take_profit_distance, size=trade_size)
            elif side == "SELL":
                self.sell(sl=self.data.Close[-1] + stop_loss_distance, tp=self.data.Close[-1] - take_profit_distance, size=trade_size)

if __name__ == '__main__':
    # ... (binance_client, symbol_to_optimize 등 설정은 동일) ...
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    symbol_to_optimize = "BTCUSDT"
    initial_cash = 10_000
    
    print(f"\n🚀 {symbol_to_optimize} 자동 최적화 파이프라인 시작...")
    klines_data = fetch_klines(binance_client, symbol_to_optimize, "4h", limit=1000)

    if klines_data is None or klines_data.empty:
        sys.exit("데이터를 가져올 수 없어 최적화를 종료합니다.")

    klines_data.columns = [col.capitalize() for col in klines_data.columns]
    OptoRunner.symbol = symbol_to_optimize
    bt = FractionalBacktest(klines_data, OptoRunner, cash=initial_cash, commission=.002, margin=1/10)
    
    print("🔬 파라미터 최적화 실행 중... (시간이 소요될 수 있습니다)")
    stats = bt.optimize(
        open_threshold=range(8, 20, 2),
        risk_reward_ratio=[1.5, 2.0, 2.5, 3.0],
        sl_atr_multiplier=[1.0, 1.5, 2.0],
        maximize='Calmar Ratio',
        constraint=lambda p: p.risk_reward_ratio > p.sl_atr_multiplier
    )
    
    print("\n✅ 최적화 완료! 최상의 파라미터 조합:")
    print(stats._strategy)

    # --- ▼▼▼ [수정] 파일 저장 경로 및 로직 개선 ▼▼▼ ---
    best_params = stats._strategy
    
    # 1. optimal_settings.json 파일은 프로젝트 최상위 경로에 저장
    results_file = os.path.join(project_root, "optimal_settings.json")
    market_regime = "BULL" # 예시: 'BULL' 시장에 대한 결과 저장

    try:
        with open(results_file, 'r', encoding='utf-8') as f:
            all_settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        all_settings = {"BULL": {}, "BEAR": {}, "SIDEWAYS": {}}

    all_settings[market_regime][symbol_to_optimize] = {
        "OPEN_TH": best_params.open_threshold,
        "RR_RATIO": best_params.risk_reward_ratio,
        "SL_ATR_MULTIPLIER": best_params.sl_atr_multiplier,
        "OPTIMIZED_METRIC": "Calmar Ratio",
        "VALUE": round(stats['Calmar Ratio'], 4)
    }

    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump(all_settings, f, indent=2, ensure_ascii=False)
    print(f"\n💾 최적화 결과를 {results_file} 파일에 저장했습니다.")

    # 2. HTML 결과 보고서는 별도의 'optimizations' 폴더에 저장
    optimization_results_folder = os.path.join("local_backtesting", "results", "optimizations")
    os.makedirs(optimization_results_folder, exist_ok=True)
    report_filename = os.path.join(optimization_results_folder, f"{symbol_to_optimize}_optimization_report.html")
    
    bt.plot(filename=report_filename, open_browser=False)
    print(f"📈 상세 리포트를 저장했습니다: {report_filename}")
    # --- ▲▲▲ [수정] ---