# 파일명: analysis/confluence_engine.py (Phase 4 - 동적 파라미터 적용 최종 수정본)

from __future__ import annotations
import math
from typing import Dict, Tuple, Optional, List
import pandas as pd
from binance.client import Client
import requests
import statistics

from . import data_fetcher, indicator_calculator
from .core_strategy import diagnose_market_regime
# 'MarketRegime'은 이제 technical한 분석에만 사용하므로 이름을 명확히 합니다.
from .core_strategy import MarketRegime as TechnicalRegime
from .macro_analyzer import MacroAnalyzer, MacroRegime as GlobalMacroRegime
from .strategies.trend_strategy import TrendStrategy
from .strategies.oscillator_strategy import OscillatorStrategy
from .strategies.comprehensive_strategy import ComprehensiveStrategy
from .strategies.signal_filter_strategy import SignalFilterStrategy
from core.config_manager import config

class ConfluenceEngine:
    """[Phase 4] 기술적 분석, 거시 경제 분석, 동적 파라미터를 통합하여 최종 결정을 내리는 '두뇌' 모듈."""

    def __init__(self, client: Client):
        self.client = client
        self.fear_and_greed_index = 50
        self.macro_analyzer = MacroAnalyzer()

        strategy_classes = {
            "TrendStrategy": TrendStrategy,
            "OscillatorStrategy": OscillatorStrategy,
            "ComprehensiveStrategy": ComprehensiveStrategy,
        }
        self.strategies = []
        for name, cls in strategy_classes.items():
            strategy_config = config.strategy_configs.get(name, {})
            if strategy_config.get("enabled", False):
                self.strategies.append(cls(params=strategy_config))
        self.filter = SignalFilterStrategy()
        print(f"✅ [Phase 4] {len(self.strategies)}개 분석 전략, 1개 신호 필터, 1개 거시 분석기가 로드되었습니다.")

    def analyze_and_decide(self, symbol: str, recent_scores: List[float], market_regime: str) -> Tuple[Optional[str], str, Optional[dict]]:
        """
        모든 분석을 종합하여 최종 매매 방향, 결정 사유, 주문 컨텍스트를 반환합니다.
        market_regime 인자를 받아 동적 파라미터를 적용합니다.
        """
        analysis_result = self.analyze_symbol(symbol)
        if not analysis_result:
            return None, f"[{symbol}]: 데이터 분석 실패.", None

        # --- ▼▼▼ [수정] 기술적 분석 기반의 시장 진단 로직 (기존 로직 유지) ▼▼▼ ---
        final_score, _, tf_rows, _, _, _ = analysis_result
        four_hour_row = tf_rows.get("4h")
        if four_hour_row is None:
            return None, f"[{symbol}]: 핵심 데이터(4h) 부족.", None

        market_data_for_diag = pd.Series({
            'adx_4h': four_hour_row.get('ADX_14'),
            'is_above_ema200_1d': tf_rows.get("1d", {}).get('close') > tf_rows.get("1d", {}).get('EMA_200')
        })
        technical_regime = diagnose_market_regime(market_data_for_diag, config.market_regime_adx_th)
        # --- ▲▲▲ [수정] ---

        if technical_regime not in [TechnicalRegime.BULL_TREND, TechnicalRegime.BEAR_TREND]:
            return None, f"[{symbol}]: 기술적 횡보장({technical_regime.value}). 관망.", None

        if len(recent_scores) < config.trend_entry_confirm_count:
            return None, f"[{symbol}]: 신호 부족({len(recent_scores)}/{config.trend_entry_confirm_count}). 관망.", None

        avg_score = statistics.mean(recent_scores)
        std_dev = statistics.pstdev(recent_scores) if len(recent_scores) > 1 else 0

        # --- ▼▼▼ [수정] 중복 코드 제거 및 동적 파라미터 정상 적용 ---
        # 외부에서 전달받은 market_regime을 사용하여 최적 파라미터를 가져옵니다.
        params = config.get_strategy_params(symbol, market_regime)
        open_threshold = params.get('open_th', 12.0)

        side = None
        # 기술적 분석이 추세장일 때만 진입 고려
        if technical_regime == TechnicalRegime.BULL_TREND and avg_score >= open_threshold and std_dev <= config.quality_max_std_dev:
            side = "BUY"
        elif technical_regime == TechnicalRegime.BEAR_TREND and abs(avg_score) >= open_threshold and std_dev <= config.quality_max_std_dev:
            side = "SELL"
        # --- ▲▲▲ [수정] ---

        if not side:
            return None, f"[{symbol}]: 신호 품질 미달(Avg:{avg_score:.1f}, Th:{open_threshold}). 관망.", None

        # 신호 필터링 적용
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
        """특정 타임프레임의 전체 지표가 포함된 DataFrame을 반환합니다."""
        try:
            df = data_fetcher.fetch_klines(self.client, symbol, timeframe, limit=200)
            if df is None or df.empty: return None
            return indicator_calculator.calculate_all_indicators(df)
        except Exception:
            return None

    def analyze_symbol(self, symbol: str) -> Optional[Tuple[float, Dict, Dict, Dict, int, str]]:
        """한 심볼에 대한 전체 기술적 분석을 수행하고 모든 관련 데이터를 튜플로 반환합니다."""
        try:
            self._fetch_fear_and_greed_index()

            tf_data = {}
            for timeframe in config.analysis_timeframes:
                df = data_fetcher.fetch_klines(self.client, symbol, timeframe, limit=200)
                if df is None or df.empty: continue
                indicators = indicator_calculator.calculate_all_indicators(df)
                if indicators is None or indicators.empty: continue
                tf_data[timeframe] = indicators

            if not tf_data: return None

            tf_scores = {tf: self._calculate_tactical_score(data)[0] for tf, data in tf_data.items()}
            tf_score_breakdowns = {tf: self._calculate_tactical_score(data)[1] for tf, data in tf_data.items()}
            tf_rows = {tf: data.iloc[-1] for tf, data in tf_data.items()}

            weights = config.tf_vote_weights
            final_score = sum(tf_scores.get(tf, 0) * (weights[i] if i < len(weights) else 1.0) for i, tf in enumerate(config.analysis_timeframes))

            confluence_signal = ""
            if (tf_scores.get("4h", 0) > 0 and tf_scores.get("1d", 0) > 0) or \
               (tf_scores.get("4h", 0) < 0 and tf_scores.get("1d", 0) < 0):
                final_score *= 1.2
                confluence_signal = "📈 4h-1d 추세 동조!"

            if (self.fear_and_greed_index <= 25 and final_score > 0) or \
               (self.fear_and_greed_index >= 75 and final_score < 0):
                final_score *= 1.2

            return final_score, tf_scores, tf_rows, tf_score_breakdowns, self.fear_and_greed_index, confluence_signal
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
        last = df.iloc[-1]
        all_scores = {}

        for strategy in self.strategies:
            all_scores.update(strategy.analyze(df))

        all_scores["다이버"] = self._find_rsi_divergence(df)
        all_scores["BB"] = 0

        bbu_col = next((c for c in df.columns if c.startswith('BBU_')), None)
        bbl_col = next((c for c in df.columns if c.startswith('BBL_')), None)
        bbb_col = next((c for c in df.columns if c.startswith('BBB_')), None)

        if all([bbu_col, bbl_col, bbb_col]) and last[bbb_col] < df[bbb_col].rolling(90).quantile(0.05).iloc[-1]:
            if last['close'] > last[bbu_col]:
                all_scores["BB"] = 3
            elif last['close'] < last[bbl_col]:
                all_scores["BB"] = -3

        return sum(all_scores.values()), all_scores

    def _find_rsi_divergence(self, df: pd.DataFrame, lookback: int = 14) -> int:
        recent = df.tail(lookback)
        if len(recent) < 2: return 0
        if recent.iloc[-1]['low'] < recent.iloc[0]['low'] and recent.iloc[-1]['RSI_14'] > recent.iloc[0]['RSI_14']: return 5
        if recent.iloc[-1]['high'] > recent.iloc[0]['high'] and recent.iloc[-1]['RSI_14'] < recent.iloc[0]['RSI_14']: return -5
        return 0

    def extract_atr(self, tf_rows: dict, primary_tf: str = "4h") -> float:
        row = tf_rows.get(primary_tf)
        if row is None: return 0.0
        val = row.get("ATRr_14") or row.get("ATR_14")
        return val if isinstance(val, (int, float)) and not math.isnan(val) else 0.0
