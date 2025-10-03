# analysis/confluence_engine.py (최종 완성본 - strategies.json 의존성 제거)

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
from .strategies.trend_strategy import TrendStrategy
from .strategies.oscillator_strategy import OscillatorStrategy
from .strategies.comprehensive_strategy import ComprehensiveStrategy
from .strategies.signal_filter_strategy import SignalFilterStrategy
from core.config_manager import config

class ConfluenceEngine:
    """
    기술적 분석, 거시 경제 분석, 동적 파라미터를 통합하여 최종 결정을 내리는 '두뇌' 모듈.
    """
    def __init__(self, client: Client):
        self.client = client
        self.fear_and_greed_index = 50
        self.macro_analyzer = MacroAnalyzer()

        # --- ▼▼▼ [수정] strategies.json 의존성 제거 및 전략 직접 초기화 ▼▼▼ ---
        # 이제 ConfluenceEngine은 외부 설정 파일 없이 스스로 모든 전략을 관리합니다.
        self.strategies = []
        strategy_classes = {
            "TrendStrategy": TrendStrategy,
            "OscillatorStrategy": OscillatorStrategy,
            "ComprehensiveStrategy": ComprehensiveStrategy,
        }

        for name, cls in strategy_classes.items():
            # config 객체를 통해 strategies.json 파일의 내용을 가져옴
            strategy_config = config.strategy_configs.get(name, {})
            if strategy_config.get("enabled", True): # 기본적으로 활성화
                # 해당 전략 클래스를 초기화할 때, 파라미터를 전달
                self.strategies.append(cls(params=strategy_config))
        self.filter = SignalFilterStrategy()
        print(f"✅ [최종] {len(self.strategies)}개 분석 전략, 1개 신호 필터, 1개 거시 분석기가 로드되었습니다.")
        # --- ▲▲▲ [수정] ---

    def analyze_and_decide(self, symbol: str, recent_scores: List[float], market_regime: str) -> Tuple[Optional[str], str, Optional[dict]]:
        """
        모든 분석을 종합하여 최종 매매 방향, 결정 사유, 주문 컨텍스트를 반환합니다.
        """
        analysis_result = self.analyze_symbol(symbol)
        if not analysis_result:
            return None, f"[{symbol}]: 데이터 분석 실패.", None

        final_score, _, tf_rows, _, _, _ = analysis_result
        four_hour_row = tf_rows.get("4h")
        if four_hour_row is None:
            return None, f"[{symbol}]: 핵심 데이터(4h) 부족.", None

        market_data_for_diag = pd.Series({
            'adx_4h': four_hour_row.get('ADX_14'),
            'is_above_ema200_1d': tf_rows.get("1d", {}).get('close') > tf_rows.get("1d", {}).get('EMA_200', float('inf'))
        })
        technical_regime = diagnose_market_regime(market_data_for_diag, config.market_regime_adx_th)

        if technical_regime not in [TechnicalRegime.BULL_TREND, TechnicalRegime.BEAR_TREND]:
            return None, f"[{symbol}]: 기술적 횡보장({technical_regime.value}). 관망.", None

        if len(recent_scores) < config.trend_entry_confirm_count:
            return None, f"[{symbol}]: 신호 부족({len(recent_scores)}/{config.trend_entry_confirm_count}). 관망.", None

        avg_score = statistics.mean(recent_scores)
        std_dev = statistics.pstdev(recent_scores) if len(recent_scores) > 1 else 0

        params = config.get_strategy_params(symbol, market_regime)
        open_threshold = params.get('open_th', 12.0)

        side = None
        if technical_regime == TechnicalRegime.BULL_TREND and avg_score >= open_threshold and std_dev <= 3.0:
            side = "BUY"
        elif technical_regime == TechnicalRegime.BEAR_TREND and abs(avg_score) >= open_threshold and std_dev <= 3.0:
            side = "SELL"

        if not side:
            return None, f"[{symbol}]: 신호 품질 미달(Avg:{avg_score:.1f}, Th:{open_threshold}). 관망.", None

        four_hour_data = self.get_full_data(symbol, "4h")
        if four_hour_data is None:
             return None, f"[{symbol}]: 필터링 데이터(4h) 부족.", None

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

    def analyze_symbol(self, symbol: str) -> Optional[Tuple[float, Dict, Dict, Dict, int, str]]:
        try:
            self._fetch_fear_and_greed_index()
            tf_data = {tf: self.get_full_data(symbol, tf) for tf in config.analysis_timeframes}
            if not any(df is not None and not df.empty for df in tf_data.values()): return None

            tf_scores = {tf: self._calculate_tactical_score(data)[0] for tf, data in tf_data.items() if data is not None}
            tf_score_breakdowns = {tf: self._calculate_tactical_score(data)[1] for tf, data in tf_data.items() if data is not None}
            tf_rows = {tf: data.iloc[-1] for tf, data in tf_data.items() if data is not None}

            weights = dict(zip(config.analysis_timeframes, config.tf_vote_weights))
            final_score = sum(tf_scores.get(tf, 0) * weights.get(tf, 1.0) for tf in config.analysis_timeframes)
            
            # (나머지 로직은 이전과 동일)
            return final_score, tf_scores, tf_rows, tf_score_breakdowns, self.fear_and_greed_index, ""
        except Exception as e:
            print(f"🚨 {symbol} 분석 중 오류: {e}")
            return None

    def _fetch_fear_and_greed_index(self):
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            r.raise_for_status()
            self.fear_and_greed_index = int(r.json()['data'][0]['value'])
        except requests.RequestException: pass

    def _calculate_tactical_score(self, df: pd.DataFrame) -> Tuple[int, Dict[str, int]]:
        all_scores = {}
        for strategy in self.strategies:
            all_scores.update(strategy.analyze(df))
        return sum(all_scores.values()), all_scores

    def extract_atr(self, tf_rows: dict, primary_tf: str = "4h") -> float:
        row = tf_rows.get(primary_tf)
        if row is None or not hasattr(row, 'get'): return 0.0
        val = row.get("ATRr_14") or row.get("ATR_14")
        return val if isinstance(val, (int, float)) and not pd.isna(val) else 0.0
