# analysis/confluence_engine.py (V11 수정)

from __future__ import annotations
import math
import os
from typing import Dict, Tuple, Optional, List
import pandas as pd
import pandas_ta as ta
from binance.client import Client
import requests
import statistics

from . import data_fetcher, indicator_calculator
from .core_strategy import diagnose_market_regime, MarketRegime as TechnicalRegime
from .macro_analyzer import MacroAnalyzer, MacroRegime, MacroState
from .strategies.base_strategy import BaseStrategy
from .strategies.trend_strategy import TrendStrategy
from .strategies.oscillator_strategy import OscillatorStrategy
from .strategies.comprehensive_strategy import ComprehensiveStrategy
from .strategies.signal_filter_strategy import SignalFilterStrategy
from .strategies.volatility_strategy import VolatilityStrategy
from .strategies.counter_trend_strategy import CounterTrendStrategy
from .strategies.momentum_strategy import MomentumStrategy
from .strategies.order_block_strategy import OrderBlockStrategy

# [ANCHOR:SNAPSHOT_HOOKS]
SNAPSHOT = None  # macro_analyzer.PriceSnapshot 인스턴스 주입용
USE_SNAPSHOT = os.getenv("MACRO_SNAPSHOT_HOOK", "1") == "1"

def set_price_snapshot(snapshot):
    """macro_analyzer.MacroAnalyzer().snapshot을 주입한다."""
    global SNAPSHOT
    SNAPSHOT = snapshot

def _last_row_aligned(symbol: str, fallback_df):
    """스냅샷이 있으면 해당 날짜의 레코드, 없으면 fallback_df의 마지막 행."""
    if USE_SNAPSHOT and SNAPSHOT is not None:
        row = SNAPSHOT.last_row(symbol)
        if row is not None:
            return row
    # fallback: 기존 방식 유지
    return fallback_df.iloc[-1] if fallback_df is not None and not fallback_df.empty else None

def _last_close_aligned(symbol: str, fallback_df):
    row = _last_row_aligned(symbol, fallback_df)
    return None if row is None else (row["Close"].item() if hasattr(row["Close"], "item") else row["Close"])


class ConfluenceEngine:
    """
    기술적 분석, 거시 경제 분석, 동적 파라미터를 통합하여 최종 결정을 내리는 '두뇌' 모듈.
    V11: __init__ 메소드가 strategy_configs를 인자로 받을 수 있도록 수정.
    """
    # ▼▼▼ [핵심 수정] __init__ 생성자에 strategy_configs 인자 추가 ▼▼▼
    def __init__(self, client: Client, config: any, strategy_configs: dict | None = None):
        self.client = client
        self.config = config
        self.fear_and_greed_index = 50
        self.macro_analyzer = MacroAnalyzer(self.config)
        self._strategy_cache: Dict[str, List[BaseStrategy]] = {}
        self.filter = SignalFilterStrategy(self.config)
        
        # backtest_optimizer에서 직접 전략 설정을 주입할 경우를 위한 로직
        self._external_strategy_configs = strategy_configs
        
        print(f"✅ ConfluenceEngine: 동적 전략 로딩 모드로 초기화되었습니다.")

    def _determine_strategic_bias(self, symbol: str) -> str:
        """
        [Top-Down Analysis] 월봉/주봉 데이터를 분석하여 '대전략'을 결정합니다.
        - 월봉 EMA(12) > EMA(24) : 장기 강세
        - 주봉 EMA(20) > EMA(50) : 중기 강세
        두 조건을 모두 만족해야 '매수 우위'로 판단합니다.
        """
        try:
            # 1. 월봉 데이터 분석 (장기 추세)
            df_monthly = data_fetcher.fetch_klines(self.client, symbol, '1M', limit=50)
            if df_monthly is not None and not df_monthly.empty:
                df_monthly['EMA_12'] = ta.ema(df_monthly['close'], length=12)
                df_monthly['EMA_24'] = ta.ema(df_monthly['close'], length=24)
                last_monthly = df_monthly.iloc[-1]
                is_monthly_bull = last_monthly['EMA_12'] > last_monthly['EMA_24']
            else:
                is_monthly_bull = False # 데이터 없으면 보수적으로 판단

            # 2. 주봉 데이터 분석 (중기 추세)
            df_weekly = data_fetcher.fetch_klines(self.client, symbol, '1w', limit=100)
            if df_weekly is not None and not df_weekly.empty:
                df_weekly['EMA_20'] = ta.ema(df_weekly['close'], length=20)
                df_weekly['EMA_50'] = ta.ema(df_weekly['close'], length=50)
                last_weekly = df_weekly.iloc[-1]
                is_weekly_bull = last_weekly['EMA_20'] > last_weekly['EMA_50']
            else:
                is_weekly_bull = False

            # 3. 최종 대전략 결정
            if is_monthly_bull and is_weekly_bull:
                return "BULLISH" # 매수 우위
            elif not is_monthly_bull and not is_weekly_bull:
                return "BEARISH" # 매도 우위
            else:
                return "NEUTRAL" # 중립

        except Exception as e:
            print(f"🚨 대전략 분석 중 오류 발생: {e}")
            return "NEUTRAL" # 오류 발생 시 중립으로 폴백

    def _get_strategies_for_regime(self, market_regime: str) -> List[BaseStrategy]:
        """
        주어진 시장 국면에 맞는 전략 객체 리스트를 반환합니다. (캐싱 적용)
        """
        regime_upper = market_regime.upper()
        if regime_upper in self._strategy_cache:
            return self._strategy_cache[regime_upper]

        strategy_classes = {
            "TrendStrategy": TrendStrategy,
            "OscillatorStrategy": OscillatorStrategy,
            "ComprehensiveStrategy": ComprehensiveStrategy,
            "VolatilityStrategy": VolatilityStrategy,
            "CounterTrendStrategy": CounterTrendStrategy,
            "MomentumStrategy": MomentumStrategy,
            "OrderBlockStrategy": OrderBlockStrategy,
        }
        
        # 최적화 프로그램에서 설정을 직접 주입했는지 확인
        if self._external_strategy_configs:
            current_strategy_configs = self._external_strategy_configs
        else:
            # 그렇지 않으면 config 파일에서 로드
            current_strategy_configs = self.config.get_strategy_configs(regime_upper)
            if not current_strategy_configs:
                current_strategy_configs = self.config.get_strategy_configs("BULL")

        created_strategies = []
        for name, cls in strategy_classes.items():
            # config 파일 기반이든, 외부 주입이든, 해당 전략의 설정값을 찾음
            strategy_config = current_strategy_configs.get(name, {})
            # enabled 키가 없거나 True일 경우에만 전략을 활성화
            if strategy_config.get("enabled", True):
                created_strategies.append(cls(params=strategy_config))
        
        self._strategy_cache[regime_upper] = created_strategies
        print(f"📈 [{regime_upper}] 국면 전략 생성 및 캐싱 완료 ({len(created_strategies)}개)")
        return created_strategies
    # ▲▲▲ [핵심 수정] ▲▲▲

    def _calculate_tactical_score(self, df: pd.DataFrame, strategies: List[BaseStrategy]) -> Tuple[int, Dict[str, int]]:
        """전략 리스트를 인자로 받아 점수를 계산합니다."""
        all_scores = {}
        for strategy in strategies:
            all_scores.update(strategy.analyze(df))
        return sum(all_scores.values()), all_scores

    def analyze_symbol(self, symbol: str, market_regime: str) -> Optional[Tuple[float, Dict, Dict, Dict, int, str]]:
        """
        시장 국면(market_regime)을 인자로 받아 해당 국면에 맞는 전략으로 분석합니다.
        """
        try:
            self._fetch_fear_and_greed_index()
            strategies = self._get_strategies_for_regime(market_regime)
            
            tf_data = {tf: self.get_full_data(symbol, tf) for tf in self.config.analysis_timeframes}
            if not any(df is not None and not df.empty for df in tf_data.values()): return None

            tf_scores = {tf: self._calculate_tactical_score(data, strategies)[0] for tf, data in tf_data.items() if data is not None}
            tf_score_breakdowns = {tf: self._calculate_tactical_score(data, strategies)[1] for tf, data in tf_data.items() if data is not None}
            tf_rows = {tf: data.iloc[-1] for tf, data in tf_data.items() if data is not None}
            
            weights = dict(zip(self.config.analysis_timeframes, self.config.tf_vote_weights))
            final_score = sum(tf_scores.get(tf, 0) * weights.get(tf, 1.0) for tf in self.config.analysis_timeframes)
            
            return final_score, tf_scores, tf_rows, tf_score_breakdowns, self.fear_and_greed_index, ""
        except Exception as e:
            print(f"🚨 {symbol} 분석 중 오류: {e}")
            return None

    def analyze_and_decide(self, symbol: str, recent_scores: List[float], market_regime: str) -> Tuple[Optional[str], str, Optional[dict]]:
        """
        [최종 버전] 월봉/주봉으로 '대전략'을 결정하고, 그에 따라 진입 임계값을 동적으로 조절하는 최종 의사결정 로직.
        """
        analysis_result = self.analyze_symbol(symbol, market_regime)
        if not analysis_result:
            return None, f"[{symbol}]: 데이터 분석 실패.", None

        avg_score = statistics.mean(recent_scores)
        _, _, tf_rows, _, _, _ = analysis_result

        # 1. 계층적 합의 1단계: 장기 추세로 '대전략' 결정
        strategic_bias = self._determine_strategic_bias(symbol)

        # 2. 동적 임계값(Dynamic Threshold) 적용
        params = self.config.get_strategy_params(symbol, market_regime)
        base_threshold = params.get('open_th', 12.0)

        buy_threshold = base_threshold
        sell_threshold = -base_threshold

        if strategic_bias == "BULLISH":
            buy_threshold *= 0.8  # 매수 기준 완화 (80%)
            sell_threshold *= 1.5 # 매도 기준 강화 (150%)
            print(f"   - [동적 임계값]  bullish bias 적용: BUY < {buy_threshold:.1f}, SELL < {sell_threshold:.1f}")
        elif strategic_bias == "BEARISH":
            buy_threshold *= 1.5  # 매수 기준 강화 (150%)
            sell_threshold *= 0.8 # 매도 기준 완화 (80%)
            print(f"   - [동적 임계값] bearish bias 적용: BUY < {buy_threshold:.1f}, SELL < {sell_threshold:.1f}")

        # 3. 계층적 합의 2단계: 단기 신호로 최종 진입 결정
        side = None
        if avg_score >= buy_threshold:
            side = "BUY"
        elif avg_score <= sell_threshold:
            side = "SELL"

        if not side:
            return None, f"[{symbol}]: 신호 품질 미달(Avg:{avg_score:.1f}, Bias:{strategic_bias}, Th:{buy_threshold:.1f}/{sell_threshold:.1f}). 관망.", None

        # 4. 신호 필터링 (기존 로직 유지)
        four_hour_data = self.get_full_data(symbol, "4h")
        if four_hour_data is None: return None, f"[{symbol}]: 필터링 데이터(4h) 부족.", None

        filter_result = self.filter.analyze(four_hour_data)
        if not filter_result["is_valid"]:
            return None, f"[{symbol}]: 신호 필터링됨 ({filter_result['reason']}). 관망.", None

        decision_reason = f"🚀 [{symbol}] {side} 진입! (Avg: {avg_score:.1f}, 대전략: {strategic_bias})"
        entry_context = {"avg_score": avg_score, "entry_atr": self.extract_atr(tf_rows)}
        return side, decision_reason, entry_context
    
    def get_full_data(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        try:
            df = data_fetcher.fetch_klines(self.client, symbol, timeframe, limit=200)
            if df is None or df.empty: return None
            return indicator_calculator.calculate_all_indicators(df)
        except Exception:
            return None

    def _fetch_fear_and_greed_index(self):
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            r.raise_for_status()
            self.fear_and_greed_index = int(r.json()['data'][0]['value'])
        except requests.RequestException: pass

    def extract_atr(self, tf_rows: dict, primary_tf: str = "4h") -> float:
        row = tf_rows.get(primary_tf)
        if row is None or not hasattr(row, 'get'): return 0.0
        val = row.get("ATRr_14") or row.get("ATR_14")
        return val if isinstance(val, (int, float)) and not pd.isna(val) else 0.0
