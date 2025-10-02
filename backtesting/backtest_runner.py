# backtesting/backtest_runner.py (V23 - 최신 두뇌 탑재)
import os
import pandas as pd
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
import sys, os, contextlib, io
from collections import deque

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 이제 ConfluenceEngine을 직접 사용합니다.
from analysis.confluence_engine import ConfluenceEngine
from analysis.data_fetcher import fetch_klines
from core.config_manager import config
from backtesting.performance_visualizer import create_performance_report

class StrategyRunner(Strategy):
    # 최적화할 파라미터들
    open_threshold = 12.0
    risk_reward_ratio = 2.5
    trend_entry_confirm_count = 3 # 신호 품질 검증을 위한 파라미터

    def init(self):
        # 1. '두뇌'인 ConfluenceEngine을 생성합니다.
        mock_client = Client("", "")
        self.engine = ConfluenceEngine(mock_client)
        
        # 2. 최근 N개의 점수를 저장할 공간을 만듭니다.
        self.recent_scores = deque(maxlen=self.trend_entry_confirm_count)

    def next(self):
        # --- 1. 데이터 분석 ---
        # 백테스팅 환경에서는 단일 타임프레임(1d)만 분석합니다.
        analysis_result = self.engine.analyze_symbol(self.data.df.name)
        if not analysis_result: return

        final_score, _, _, _, _, _ = analysis_result
        self.recent_scores.append(final_score)

        # --- 2. '두뇌'에게 최종 결정 요청 ---
        # main.py와 동일하게 최근 점수 리스트를 전달합니다.
        side, reason, context = self.engine.analyze_and_decide(self.data.df.name, list(self.recent_scores))

        # --- 3. 결정에 따라 주문 실행 ---
        if side and not self.position:
            entry_price = self.data.Close[-1]
            entry_atr = context.get('entry_atr', 0)
            if entry_atr <= 0: return

            stop_loss_distance = entry_atr * config.sl_atr_multiplier
            take_profit_distance = stop_loss_distance * self.risk_reward_ratio
            
            if side == "BUY":
                sl_price = entry_price - stop_loss_distance
                tp_price = entry_price + take_profit_distance
                if sl_price > 0: self.buy(sl=sl_price, tp=tp_price, size=0.1)
            elif side == "SELL":
                sl_price = entry_price + stop_loss_distance
                tp_price = entry_price - take_profit_distance
                if tp_price > 0: self.sell(sl=sl_price, tp=tp_price, size=0.1)

if __name__ == '__main__':
    # ... (상단 binance_client, symbol, klines_data 부분은 동일) ...

    if klines_data is not None and not klines_data.empty:
        klines_data.columns = [col.capitalize() for col in klines_data.columns]
        bt = Backtest(klines_data, StrategyRunner, cash=10_000, commission=.002)

        # ▼▼▼ [개선] 결과물 저장 폴더 지정 ▼▼▼
        results_folder = "backtesting/results"
        os.makedirs(results_folder, exist_ok=True) # 폴더가 없으면 자동 생성

        chart_filename = os.path.join(results_folder, f"{symbol}_performance_chart.png")
        report_filename = os.path.join(results_folder, f"{symbol}_backtest_report.html")
        # ▲▲▲ [개선] ▲▲▲

        stats = bt.optimize(
            open_threshold=range(8, 15, 2),
            risk_reward_ratio=[2.0, 2.5, 3.0],
            maximize='Equity Final [$]'
        )

        print(f"\n--- [{symbol}] 최적화 결과 ---")
        print("\n✅ 가장 성과가 좋았던 파라미터 조합:")
        print(stats._strategy)

        report_text, chart_buffer = create_performance_report(stats)

        print("\n" + report_text)

        if chart_buffer:
            with open(chart_filename, "wb") as f: # <--- 수정된 경로 사용
                f.write(chart_buffer.getbuffer())
            print(f"\n📈 {chart_filename} 파일에 상세 차트가 저장되었습니다.") # <--- 수정된 경로 사용

        bt.plot(filename=report_filename) # <--- 수정된 경로 사용
        print(f"\n📄 {report_filename} 파일에 상세 리포트가 저장되었습니다.") # <--- 수정된 경로 사용
