# local_backtesting/backtest_runner.py (최종 수정본)

import pandas as pd
from backtesting import Strategy, Backtest
from binance.client import Client
from collections import deque
import sys
import os

# ▼▼▼ 프로젝트 루트 폴더 경로 추가 ▼▼▼
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# ▲▲▲ 프로젝트 루트 폴더 경로 추가 ▲▲▲

from analysis import indicator_calculator
from analysis.confluence_engine import ConfluenceEngine
from core.config_manager import config
from analysis.data_fetcher import fetch_klines
from local_backtesting.performance_visualizer import create_performance_report

class StrategyRunner(Strategy):
    open_threshold = 12.0
    risk_reward_ratio = 2.5
    trend_entry_confirm_count = 3

    def init(self):
        print("[StrategyRunner] init() 메소드 시작.")
        self.engine = ConfluenceEngine(Client("", ""))
        self.recent_scores = deque(maxlen=self.trend_entry_confirm_count)
        
        if self.data.df.empty:
            print("[StrategyRunner] 오류: init()에서 데이터프레임이 비어있습니다.")
            return

        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)
        print(f"[StrategyRunner] init() 완료. 지표 계산 완료. (총 {len(self.indicators)}개 데이터)")

    def next(self):
        # [수정] 현재 데이터의 인덱스를 가져오는 올바른 방법으로 변경
        current_index = len(self.data) - 1

        if current_index < self.trend_entry_confirm_count:
            return

        # 현재 시점까지의 데이터로 점수 계산
        current_indicators = self.indicators.iloc[:current_index + 1]
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
            last_row = self.indicators.iloc[current_index] # 현재 캔들의 지표
            entry_atr = last_row.get("atrr_14", last_row.get("atr_14", 0)) # 컬럼명이 소문자로 통일되었을 가능성에 대비
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
        # backtesting 라이브러리는 대문자 컬럼명을 사용하므로 여기서 변경
        klines_data.columns = [col.capitalize() for col in klines_data.columns]
        
        bt = Backtest(klines_data, StrategyRunner, cash=10_000, commission=.002)

        results_folder = os.path.join("local_backtesting", "results")
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
