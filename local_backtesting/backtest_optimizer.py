# local_backtesting/backtest_optimizer.py (지표 파라미터 전체 최적화 버전)

import multiprocessing
from backtesting import backtesting
backtesting.Pool = multiprocessing.Pool
import pandas as pd
import json
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
from collections import deque
import sys
import os
from tqdm import tqdm
import itertools

# --- 프로젝트 경로 설정 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from analysis import indicator_calculator, data_fetcher
from analysis.confluence_engine import ConfluenceEngine
from analysis.macro_analyzer import MacroAnalyzer
from core.config_manager import config

# ... (segment_data_by_regime 함수는 이전과 동일) ...
def segment_data_by_regime(klines_df: pd.DataFrame, macro_data: dict) -> dict:
    print("\n...과거 데이터 전체에 대한 거시 경제 분석을 시작합니다...")
    macro_analyzer = MacroAnalyzer()
    regime_periods = []
    
    for date in tqdm(klines_df.index, desc="과거 시장 상황 분석 중"):
        regime, _, _ = macro_analyzer.diagnose_macro_regime_for_date(date, macro_data)
        regime_periods.append(regime.name)
    
    klines_df['Regime'] = regime_periods
    
    segmented_data = {
        "BULL": klines_df[klines_df['Regime'] == 'BULL'],
        "BEAR": klines_df[klines_df['Regime'] == 'BEAR'],
        "SIDEWAYS": klines_df[klines_df['Regime'] == 'SIDEWAYS']
    }
    print("...거시 경제 분석 및 데이터 구간 선별 완료!")
    print(f"   - 강세장(BULL) 데이터: {len(segmented_data['BULL'])}개 캔들")
    print(f"   - 약세장(BEAR) 데이터: {len(segmented_data['BEAR'])}개 캔들")
    print(f"   - 횡보장(SIDEWAYS) 데이터: {len(segmented_data['SIDEWAYS'])}개 캔들")
    return segmented_data


class OptoRunner(Strategy):
    # --- ▼▼▼ [수정] 최적화할 모든 파라미터를 클래스 변수로 선언 ---
    
    # 1. 실행 조건 파라미터
    open_threshold = 12.0
    risk_reward_ratio = 2.0
    sl_atr_multiplier = 1.5
    trend_entry_confirm_count = 3

    # 2. TrendStrategy 파라미터
    ema_short = 20
    ema_long = 50
    score_strong_trend = 5

    # 3. OscillatorStrategy 파라미터 (주요 값만 선별)
    rsi_period = 14
    rsi_oversold = 30
    rsi_overbought = 70
    score_oversold = 5
    score_overbought = -5

    # 4. ComprehensiveStrategy 파라미터 (주요 값만 선별)
    score_macd_cross_up = 2
    adx_threshold = 25
    score_adx_strong = 3
    score_bb_breakout_up = 4
    score_chop_trending = 3

    # (내부 변수)
    symbol = "BTCUSDT"
    market_regime = "BULL"

    def init(self):
        # --- ▼▼▼ [수정] backtesting 프레임워크가 전달한 파라미터로 strategy_configs를 동적으로 구성 ---
        strategy_configs = {
            "TrendStrategy": {
                "enabled": True, "ema_short": int(self.ema_short), "ema_long": int(self.ema_long), 
                "score_strong_trend": int(self.score_strong_trend)
            },
            "OscillatorStrategy": {
                "enabled": True, "rsi_period": int(self.rsi_period), "rsi_oversold": int(self.rsi_oversold), 
                "rsi_overbought": int(self.rsi_overbought), "score_oversold": int(self.score_oversold),
                "score_overbought": int(self.score_overbought),
                # 나머지 오실레이터 값들은 기본값 사용 (최적화 시간 단축)
                "stoch_k": 14, "stoch_d": 3, "stoch_smooth_k": 3, "mfi_period": 14, "obv_ema_period": 20,
                "stoch_oversold": 20, "stoch_overbought": 80, "mfi_oversold": 20, "mfi_overbought": 80,
                "score_inflow": 2, "score_outflow": -2
            },
            "ComprehensiveStrategy": {
                "enabled": True, "score_macd_cross_up": int(self.score_macd_cross_up), 
                "score_macd_cross_down": -int(self.score_macd_cross_up), "adx_threshold": int(self.adx_threshold),
                "score_adx_strong": int(self.score_adx_strong), "score_bb_breakout_up": int(self.score_bb_breakout_up),
                "score_bb_breakout_down": -int(self.score_bb_breakout_up), "score_chop_trending": int(self.score_chop_trending),
                # 나머지 종합지표 값들은 기본값 사용
                "score_ichimoku_bull": 4, "score_ichimoku_bear": -4, "score_psar_bull": 3, "score_psar_bear": -3,
                "score_vortex_bull": 2, "score_vortex_bear": -2, "bb_len": 20, "bb_std": 2.0, "score_bb_squeeze": 3,
                "cci_length": 20, "cci_constant": 0.015, "cci_overbought": 100, "cci_oversold": -100,
                "score_cci_overbought": -3, "score_cci_oversold": 3, "score_cmf_positive": 2, "score_cmf_negative": -2,
                "chop_sideways_th": 60, "score_chop_sideways": -3, "stochrsi_oversold": 20, "stochrsi_overbought": 80,
                "score_stochrsi_oversold": 3, "score_stochrsi_overbought": -3, "score_trix_cross_up": 4,
                "score_trix_cross_down": -4, "score_efi_cross_up": 3, "score_efi_cross_down": -3,
                "score_kc_breakout_up": 4, "score_kc_breakout_down": -4, "score_ppo_bull": 2, "score_ppo_bear": -2
            }
        }
        self.engine = ConfluenceEngine(Client("", ""), strategy_configs=strategy_configs)
        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)
        self.recent_scores = deque(maxlen=int(self.trend_entry_confirm_count))
        # --- ▲▲▲ [수정] ---

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
            
            current_price = self.data.Close[-1]
            sl_price = current_price - stop_loss_distance if side == "BUY" else current_price + stop_loss_distance
            tp_price = current_price + take_profit_distance if side == "BUY" else current_price - take_profit_distance

            if sl_price <= 0 or tp_price <= 0: return
            if side == "BUY": self.buy(sl=sl_price, tp=tp_price, size=0.5)
            else: self.sell(sl=sl_price, tp=tp_price, size=0.5)

if __name__ == '__main__':
    backtesting.Pool = multiprocessing.Pool
    symbols_to_optimize = ["BTCUSDT", "ETHUSDT"]
    initial_cash = 10_000
    binance_client = Client(config.api_key, config.api_secret)

    # --- ▼▼▼ [수정] 2개의 설정 파일을 관리 ---
    optimal_settings_file = os.path.join(project_root, "optimal_settings.json")
    strategies_optimized_file = os.path.join(project_root, "strategies_optimized.json")

    try:
        with open(optimal_settings_file, 'r', encoding='utf-8') as f: all_settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): all_settings = {}
    
    try:
        with open(strategies_optimized_file, 'r', encoding='utf-8') as f: all_strategies = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): all_strategies = {"BULL": {}, "BEAR": {}, "SIDEWAYS": {}}
    # --- ▲▲▲ [수정] ---

    macro_analyzer_preload = MacroAnalyzer()
    preloaded_macro_data = macro_analyzer_preload.preload_all_macro_data()

    for symbol in symbols_to_optimize:
        print(f"\n\n{'='*50}\n🚀 **{symbol}** 자동 최적화 시작...\n{'='*50}")
        
        klines_data = data_fetcher.fetch_klines(binance_client, symbol, "4h", limit=1500) # 데이터 기간 확장
        if klines_data is None or len(klines_data) < 200: continue
        
        segmented_data = segment_data_by_regime(klines_data, preloaded_macro_data)

        for regime in ["BULL", "BEAR"]:
            print(f"\n--- 🔬 [{symbol}] '{regime}' 시장 구간 최적화 ---")
            regime_klines = segmented_data.get(regime)
            if regime_klines is None or len(regime_klines) < 100: # 데이터 최소 길이 증가
                print(f"'{regime}' 시장 데이터가 부족하여 최적화를 건너뜁니다.")
                continue

            regime_klines.columns = [col.capitalize() for col in regime_klines.columns]
            OptoRunner.symbol = symbol
            OptoRunner.market_regime = regime

            bt = FractionalBacktest(regime_klines, OptoRunner, cash=initial_cash, commission=.002, margin=1/10)
            
            # --- ▼▼▼ [수정] 최적화 파라미터 대폭 확장 (시간이 매우 오래 걸릴 수 있습니다) ---
            stats = bt.optimize(
                # 실행 조건 (2 × 2 × 2 × 2 = 16)
                open_threshold=[12, 16],          # 2개
                risk_reward_ratio=[2.0, 3.0],     # 2개
                sl_atr_multiplier=[1.5, 2.0],     # 2개
                trend_entry_confirm_count=[2, 3], # 2개

                # TrendStrategy (3 × 3 × 2 = 18)
                ema_short=[15, 20, 25],           # 3개
                ema_long=[45, 55, 60],            # 3개
                score_strong_trend=[4, 5],        # 2개

                # OscillatorStrategy (2 × 2 = 4)
                rsi_oversold=[25, 30],            # 2개
                score_oversold=[4, 5],            # 2개
                rsi_period=[14],                  # 1개 (고정)

                # ComprehensiveStrategy (3 × 2 × 2 = 12)
                score_macd_cross_up=[2, 3, 4],    # 3개
                adx_threshold=[20, 25],           # 2개
                score_adx_strong=[2, 3],          # 2개

                maximize='Calmar Ratio',
                # 제약조건 강화: EMA 단기 < 장기, 손익비는 손절폭보다 커야 함
                constraint=lambda p: p.ema_short < p.ema_long and p.risk_reward_ratio > p.sl_atr_multiplier 
            )
            # --- ▲▲▲ [수정] ---

            best_params = stats._strategy
            metric_name = 'Calmar Ratio'
            metric_value = stats[metric_name]

            print(f"\n--- ✅ [{symbol}/{regime}] 최적화 완료! (결과: {metric_name}={metric_value:.3f}) ---")
            
            # --- ▼▼▼ [수정] 2개의 파일에 나누어 결과 저장 ---
            if regime not in all_settings: all_settings[regime] = {}
            all_settings[regime][symbol] = {
                "OPEN_TH": int(best_params.open_threshold),
                "RR_RATIO": float(best_params.risk_reward_ratio),
                "SL_ATR_MULTIPLIER": float(best_params.sl_atr_multiplier),
                "TREND_ENTRY_CONFIRM_COUNT": int(best_params.trend_entry_confirm_count),
                "OPTIMIZED_METRIC": metric_name, "VALUE": float(round(metric_value, 4)) if not pd.isna(metric_value) else 0.0
            }
            with open(optimal_settings_file, 'w', encoding='utf-8') as f:
                json.dump(all_settings, f, indent=4, ensure_ascii=False)

            # strategies.json의 기본 구조를 불러와 최적화된 값으로 덮어쓰기
            base_strategies = config.strategy_configs
            
            # TrendStrategy 업데이트
            base_strategies["TrendStrategy"]["ema_short"] = int(best_params.ema_short)
            base_strategies["TrendStrategy"]["ema_long"] = int(best_params.ema_long)
            base_strategies["TrendStrategy"]["score_strong_trend"] = int(best_params.score_strong_trend)

            # OscillatorStrategy 업데이트
            base_strategies["OscillatorStrategy"]["rsi_period"] = int(best_params.rsi_period)
            base_strategies["OscillatorStrategy"]["rsi_oversold"] = int(best_params.rsi_oversold)
            base_strategies["OscillatorStrategy"]["rsi_overbought"] = 100 - int(best_params.rsi_oversold) # 대칭적으로 설정
            base_strategies["OscillatorStrategy"]["score_oversold"] = int(best_params.score_oversold)
            base_strategies["OscillatorStrategy"]["score_overbought"] = -int(best_params.score_oversold)

            # ComprehensiveStrategy 업데이트
            base_strategies["ComprehensiveStrategy"]["score_macd_cross_up"] = int(best_params.score_macd_cross_up)
            base_strategies["ComprehensiveStrategy"]["score_macd_cross_down"] = -int(best_params.score_macd_cross_up)
            base_strategies["ComprehensiveStrategy"]["adx_threshold"] = int(best_params.adx_threshold)
            base_strategies["ComprehensiveStrategy"]["score_adx_strong"] = int(best_params.score_adx_strong)
            
            if regime not in all_strategies: all_strategies[regime] = {}
            all_strategies[regime] = base_strategies

            with open(strategies_optimized_file, 'w', encoding='utf-8') as f:
                json.dump(all_strategies, f, indent=2, ensure_ascii=False)
            
            print(f"   💾 최적화된 실행 조건과 지표 설정을 각각 **{optimal_settings_file}**과 **{strategies_optimized_file}**에 저장했습니다.")
            # --- ▲▲▲ [수정] ---
