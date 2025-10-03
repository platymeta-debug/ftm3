# backtesting/backtest_runner.py (최종 수정본)

import pandas as pd
from backtesting import Strategy, Backtest
from binance.client import Client
from collections import deque
import sys
import os

# ▼▼▼ [오류 수정] 프로젝트 루트 폴더 경로 추가 ▼▼▼
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# ▲▲▲ [오류 수정] ▲▲▲

from analysis import indicator_calculator
from analysis.confluence_engine import ConfluenceEngine
from core.config_manager import config
from analysis.data_fetcher import fetch_klines
from backtesting.performance_visualizer import create_performance_report

class StrategyRunner(Strategy):
    open_threshold = 12.0
    risk_reward_ratio = 2.5
    trend_entry_confirm_count = 3

    def init(self):
        # ConfluenceEngine은 순수 계산용으로만 사용 (API 호출 X)
        self.engine = ConfluenceEngine(Client("", ""))
        self.recent_scores = deque(maxlen=self.trend_entry_confirm_count)
        # 백테스팅 시작 시 모든 지표를 한 번만 미리 계산하여 성능 향상
        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)

    def next(self):
        # self.i는 현재 캔들(시간)의 인덱스를 가리킴
        if self.i < self.trend_entry_confirm_count:
            return

        # 현재 시점까지의 데이터로 점수 계산 (미리 계산된 지표 사용)
        current_indicators = self.indicators.iloc[:self.i + 1]
        if current_indicators.empty: return

        final_score, _ = self.engine._calculate_tactical_score(current_indicators)
        self.recent_scores.append(final_score)

        # 진입 결정 로직
        avg_score = sum(self.recent_scores) / len(self.recent_scores)
        side = None
        if avg_score >= self.open_threshold:
            side = "BUY"
        elif avg_score <= -self.open_threshold:
            side = "SELL"

        # 주문 실행
        if side and not self.position:
            last_row = self.indicators.iloc[self.i] # 현재 캔들의 지표
            entry_atr = last_row.get("ATRr_14", last_row.get("ATR_14", 0))
            if not entry_atr or entry_atr <= 0: return

            stop_loss_distance = entry_atr * config.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio

            if side == "BUY":
                self.buy(sl=self.data.Close[-1] - stop_loss_distance,
                         tp=self.data.Close[-1] + take_profit_distance,
                         size=0.1)
            elif side == "SELL":
                self.sell(sl=self.data.Close[-1] + stop_loss_distance,
                          tp=self.data.Close[-1] - take_profit_distance,
                          size=0.1)

if __name__ == '__main__':
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    symbol = "ETHUSDT"
    print(f"\n🚀 {symbol}에 대한 로컬 백테스팅을 시작합니다...")

    klines_data = fetch_klines(binance_client, symbol, "1d", limit=500)

    if klines_data is not None and not klines_data.empty:
        klines_data.columns = [col.capitalize() for col in klines_data.columns]
        bt = Backtest(klines_data, StrategyRunner, cash=10_000, commission=.002)

        results_folder = os.path.join("backtesting", "results")
        os.makedirs(results_folder, exist_ok=True)
        chart_filename = os.path.join(results_folder, f"{symbol}_performance_chart.png")
        report_filename = os.path.join(results_folder, f"{symbol}_backtest_report.html")

        stats = bt.run()
        print(f"\n--- [{symbol}] 백테스팅 결과 ---")
        report_text, chart_buffer = create_performance_report(stats)
        print("\n" + report_text)

        if chart_buffer:
            with open(chart_filename, "wb") as f:
                f.write(chart_buffer.getbuffer())
            print(f"\n📈 {chart_filename} 파일에 상세 차트가 저장되었습니다.")

        bt.plot(filename=report_filename)
        print(f"\n📄 {report_filename} 파일에 상세 리포트가 저장되었습니다.")
