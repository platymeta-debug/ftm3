# analysis/confluence_engine.py (V8 업그레이드)

from __future__ import annotations
import math
from typing import Dict, Tuple, Optional, List
import pandas as pd
from binance.client import Client
import requests
import statistics

from . import data_fetcher, indicator_calculator
from .core_strategy import diagnose_market_regime, MarketRegime as TechnicalRegime
from .macro_analyzer import MacroAnalyzer
from .strategies.base_strategy import BaseStrategy
from .strategies.trend_strategy import TrendStrategy
from .strategies.oscillator_strategy import OscillatorStrategy
from .strategies.comprehensive_strategy import ComprehensiveStrategy
from .strategies.signal_filter_strategy import SignalFilterStrategy
from .strategies.volatility_strategy import VolatilityStrategy


class ConfluenceEngine:
    """
    기술적 분석, 거시 경제 분석, 동적 파라미터를 통합하여 최종 결정을 내리는 '두뇌' 모듈.
    V8: 시장 국면에 따라 동적으로 전략 인스턴스를 생성하고 캐싱하여 분석합니다.
    """
    def __init__(self, client: Client, config: any):
        self.client = client
        self.config = config
        self.fear_and_greed_index = 50
        self.macro_analyzer = MacroAnalyzer(self.config)
        self._strategy_cache: Dict[str, List[BaseStrategy]] = {}
        self.filter = SignalFilterStrategy(self.config)
        print(f"✅ ConfluenceEngine: 동적 전략 로딩 모드로 초기화되었습니다.")

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
            "VolatilityStrategy": VolatilityStrategy, # <-- 신규 전략 추가
        }
        
        # 설정 파일에서 해당 국면의 설정을 가져오고, 없으면 BULL을 기본값으로 사용
        strategy_configs = self.config.get_strategy_configs(regime_upper)
        if not strategy_configs:
            strategy_configs = self.config.get_strategy_configs("BULL")

        created_strategies = []
        for name, cls in strategy_classes.items():
            strategy_config = strategy_configs.get(name, {})
            if strategy_config.get("enabled", True):
                created_strategies.append(cls(params=strategy_config))
        
        self._strategy_cache[regime_upper] = created_strategies
        print(f"📈 [{regime_upper}] 국면 전략 생성 및 캐싱 완료 ({len(created_strategies)}개)")
        return created_strategies

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

            # 각 타임프레임에 대해 동일한 (국면에 맞는) 전략 세트를 적용하여 점수 계산
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
        analysis_result = self.analyze_symbol(symbol, market_regime)
        if not analysis_result:
            return None, f"[{symbol}]: 데이터 분석 실패.", None

        _, _, tf_rows, _, _, _ = analysis_result
        four_hour_row = tf_rows.get("4h")
        if four_hour_row is None:
            return None, f"[{symbol}]: 핵심 데이터(4h) 부족.", None
        
        market_data_for_diag = pd.Series({
            'adx_4h': four_hour_row.get('ADX_14'),
            'is_above_ema200_1d': tf_rows.get("1d", {}).get('close') > tf_rows.get("1d", {}).get('EMA_200', float('inf'))
        })
        technical_regime = diagnose_market_regime(market_data_for_diag, self.config.market_regime_adx_th)

        if market_regime != "SIDEWAYS" and technical_regime not in [TechnicalRegime.BULL_TREND, TechnicalRegime.BEAR_TREND]:
            return None, f"[{symbol}]: 기술적 횡보장({technical_regime.value}). 관망.", None

        if len(recent_scores) < self.config.trend_entry_confirm_count:
            return None, f"[{symbol}]: 신호 부족({len(recent_scores)}/{self.config.trend_entry_confirm_count}). 관망.", None

        avg_score = statistics.mean(recent_scores)
        std_dev = statistics.pstdev(recent_scores) if len(recent_scores) > 1 else 0
        
        params = self.config.get_strategy_params(symbol, market_regime)
        open_threshold = params.get('open_th', 12.0)

        side = None
        # 횡보장에서는 avg_score 대신 마지막 점수를 사용하고, 문턱값을 낮춤
        if market_regime == "SIDEWAYS":
            last_score = recent_scores[-1]
            sideways_th = 8.0 # 횡보장 전용 문턱값
            if last_score >= sideways_th: side = "BUY"
            elif last_score <= -sideways_th: side = "SELL"
        else: # 추세장 (BULL, BEAR)
            if technical_regime == TechnicalRegime.BULL_TREND and avg_score >= open_threshold and std_dev <= 3.0:
                side = "BUY"
            elif technical_regime == TechnicalRegime.BEAR_TREND and abs(avg_score) >= open_threshold and std_dev <= 3.0:
                side = "SELL"

        if not side:
            return None, f"[{symbol}]: 신호 품질 미달(Avg:{avg_score:.1f}, Th:{open_threshold}). 관망.", None

        four_hour_data = self.get_full_data(symbol, "4h")
        if four_hour_data is None: return None, f"[{symbol}]: 필터링 데이터(4h) 부족.", None

        filter_result = self.filter.analyze(four_hour_data)
        if not filter_result["is_valid"]:
            return None, f"[{symbol}]: 신호 필터링됨 ({filter_result['reason']}). 관망.", None

        decision_reason = f"🚀 [{symbol}] {side} 진입! (Avg: {avg_score:.1f}, Tech: {technical_regime.value})"
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
