# 파일명: analysis/confluence_engine.py (전략 모듈화 적용)

from __future__ import annotations
import math
from typing import Dict, Tuple, Optional, List
import pandas as pd
from binance.client import Client
import requests
import statistics

# --- ▼▼▼ [수정] 분리된 전략 모듈 임포트 ▼▼▼ ---
from . import data_fetcher, indicator_calculator
from .core_strategy import diagnose_market_regime, MarketRegime
from .strategies.trend_strategy import TrendStrategy
from .strategies.oscillator_strategy import OscillatorStrategy
# --- ▲▲▲ [수정] ▲▲▲ ---
from core.config_manager import config

class ConfluenceEngine:
    """[Phase 1] 개별 분석 전략을 동적으로 로드하여 종합 점수를 계산하는 통합 '두뇌' 모듈."""
    def __init__(self, client: Client):
        self.client = client
        self.fear_and_greed_index = 50

        # --- ▼▼▼ [수정] 전략 인스턴스 생성 ▼▼▼ ---
        self.strategies = [
            TrendStrategy(),
            OscillatorStrategy(),
            # 여기에 다른 전략들을 추가할 수 있습니다.
        ]
        print(f"✅ [Phase 1] {len(self.strategies)}개의 분석 전략 모듈이 로드되었습니다.")
        # --- ▲▲▲ [수정] ▲▲▲ ---

    # ... (analyze_and_decide, analyze_symbol, _fetch_fear_and_greed_index 메소드는 기존과 동일) ...
    def analyze_and_decide(self, symbol: str, recent_scores: List[float]) -> Tuple[Optional[str], str, Optional[dict]]:
        """모든 분석을 종합하여 최종 매매 방향('BUY'/'SELL'/None), 결정 사유, 주문 컨텍스트를 반환합니다."""
        analysis_result = self.analyze_symbol(symbol)
        if not analysis_result:
            return None, f"[{symbol}]: 데이터 분석 실패.", None

        _, _, tf_rows, _, _, _ = analysis_result
        daily_row = tf_rows.get("1d")
        four_hour_row = tf_rows.get("4h")
        if daily_row is None or four_hour_row is None:
            return None, f"[{symbol}]: 핵심 데이터(1d/4h) 부족.", None

        market_data_for_diag = pd.Series({
            'adx_4h': four_hour_row.get('ADX_14'),
            'is_above_ema200_1d': daily_row.get('close') > daily_row.get('EMA_200') if pd.notna(daily_row.get('EMA_200')) else False
        })
        market_regime = diagnose_market_regime(market_data_for_diag, config.market_regime_adx_th)

        if market_regime not in [MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND]:
            return None, f"[{symbol}]: 횡보장({market_regime.value}). 관망.", None

        if len(recent_scores) < config.trend_entry_confirm_count:
            return None, f"[{symbol}]: 신호 부족({len(recent_scores)}/{config.trend_entry_confirm_count}). 관망.", None

        avg_score = statistics.mean(recent_scores)
        std_dev = statistics.pstdev(recent_scores) if len(recent_scores) > 1 else 0

        side = None
        params = config.get_strategy_params(symbol)
        open_threshold = params.get('open_th', 12.0)

        if market_regime == MarketRegime.BULL_TREND and avg_score >= open_threshold and std_dev <= config.quality_max_std_dev:
            side = "BUY"
        elif market_regime == MarketRegime.BEAR_TREND and abs(avg_score) >= open_threshold and std_dev <= config.quality_max_std_dev:
            side = "SELL"

        if not side:
            return None, f"[{symbol}]: 신호 품질 미달(Avg:{avg_score:.1f}, Th:{open_threshold}). 관망.", None

        decision_reason = f"🚀 [{symbol}] {side} 진입! (Avg: {avg_score:.1f})"
        entry_context = {"avg_score": avg_score, "entry_atr": self.extract_atr(tf_rows)}
        return side, decision_reason, entry_context

    def analyze_symbol(self, symbol: str) -> Optional[Tuple[float, Dict, Dict, Dict, int, str]]:
        """한 심볼에 대한 전체 분석을 수행하고 모든 관련 데이터를 튜플로 반환합니다."""
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
        # --- ▼▼▼ [수정] 모듈화된 전략을 호출하고 결과를 종합하도록 변경 ▼▼▼ ---
        last = df.iloc[-1]
        all_scores = {}

        # 1. 로드된 모든 전략 모듈 실행
        for strategy in self.strategies:
            all_scores.update(strategy.analyze(df))

        # 2. 기존의 나머지 점수 로직 (다이버전스, 볼린저밴드 등)은 유지
        all_scores["다이버"] = self._find_rsi_divergence(df)
        all_scores["BB"] = 0 # 기본값

        bbu_col = next((c for c in df.columns if c.startswith('BBU_')), None)
        bbl_col = next((c for c in df.columns if c.startswith('BBL_')), None)
        bbb_col = next((c for c in df.columns if c.startswith('BBB_')), None)

        if all([bbu_col, bbl_col, bbb_col]) and last[bbb_col] < df[bbb_col].rolling(90).quantile(0.05).iloc[-1]:
            if last['close'] > last[bbu_col]:
                all_scores["BB"] = 3
            elif last['close'] < last[bbl_col]:
                all_scores["BB"] = -3

        return sum(all_scores.values()), all_scores
        # --- ▲▲▲ [수정] ▲▲▲ ---

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
