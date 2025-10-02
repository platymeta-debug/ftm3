# analysis/confluence_engine.py (V5 - 완전한 두뇌로 업그레이드)

from __future__ import annotations
import math
from typing import Dict, Tuple, Optional, List
import pandas as pd
from binance.client import Client
import requests
import statistics

from core.config_manager import config
from . import data_fetcher, indicator_calculator
# 공용 전략 모듈에서 시장 체제 진단 로직을 가져옵니다.
from .core_strategy import diagnose_market_regime, MarketRegime
# Signal 타입을 사용하기 위해 import 합니다.
from database.models import Signal

class ConfluenceEngine:
    """
    [V5] '두뇌' 업그레이드 버전.
    모든 분석(거시, 미시, 심리)과 최종 매매 결정 로직을 통합하여 관리합니다.
    """
    def __init__(self, client: Client):
        self.client = client
        self.fear_and_greed_index = 50 # 기본값: 중립
        print("📈 [V5] Confluence Engine이 완전한 '두뇌'로 업그레이드되었습니다.")

    # ==================================
    # 🚀 최종 판단: 모든 분석을 종합하여 매매 결정을 내립니다.
    # ==================================
    def analyze_and_decide(self, symbol: str, recent_scores: List[float]) -> Tuple[Optional[str], str, Optional[dict]]:
        """
        [V5] 모든 분석을 종합하고, main.py의 최종 결정 로직까지 수행하여
        매매 방향('BUY', 'SELL', None), 결정 사유, 그리고 주문에 필요한 컨텍스트를 반환합니다.
        """
        # --- 1. 거시 및 미시 데이터/점수 분석 ---
        analysis_result = self.analyze_symbol(symbol)
        if not analysis_result:
            return None, f"[{symbol}]: 데이터 분석에 실패하여 관망.", None

        final_score, tf_scores, tf_rows, tf_score_breakdowns, fng_index, confluence_signal = analysis_result

        # --- 2. 시장 체제 진단 (main.py 로직 이전) ---
        daily_row = tf_rows.get("1d")
        four_hour_row = tf_rows.get("4h")
        if daily_row is None or four_hour_row is None:
            return None, f"[{symbol}]: 일봉/4시간봉 데이터 부족으로 시장 진단 불가.", None

        market_data_for_diag = pd.Series({
            'adx_4h': four_hour_row.get('ADX_14'),
            'is_above_ema200_1d': daily_row.get('close') > daily_row.get('EMA_200')
        })
        market_regime = diagnose_market_regime(market_data_for_diag, config.market_regime_adx_th)

        if market_regime not in [MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND]:
            return None, f"[{symbol}]: 추세장({market_regime.value})이 아니므로 관망.", None

        # --- 3. 신호 품질 검증 (main.py 로직 이전) ---
        if len(recent_signals) < config.trend_entry_confirm_count:
            return None, f"[{symbol}]: 신호 데이터 부족({len(recent_signals)}/{config.trend_entry_confirm_count})으로 관망.", None
        
        #scores = [s.final_score for s in recent_signals]
        avg_score = statistics.mean(recent_scores)
        std_dev = statistics.pstdev(recent_scores) if len(recent_scores) > 1 else 0

        side = None
        if market_regime == MarketRegime.BULL_TREND and avg_score >= config.quality_min_avg_score and std_dev <= config.quality_max_std_dev:
            side = "BUY"
        elif market_regime == MarketRegime.BEAR_TREND and abs(avg_score) >= config.quality_min_avg_score and std_dev <= config.quality_max_std_dev:
            side = "SELL"
        
        if not side:
            return None, f"[{symbol}]: 신호 품질 기준 미달 (Avg: {avg_score:.2f}, StdDev: {std_dev:.2f}). 관망.", None
        
        # --- 4. 최종 결정 및 주문 컨텍스트 반환 ---
        # 모든 필터를 통과했으므로, 매매 결정과 주문에 필요한 데이터를 함께 반환합니다.
        decision_reason = f"🚀 [{symbol}] {side} 진입 결정! (Avg: {avg_score:.2f})"
        entry_context = {
            "avg_score": avg_score,
            "entry_atr": self.extract_atr(tf_rows),
            "signal_id": None # 백테스팅에서는 signal_id를 추적하지 않음
        }
        return side, decision_reason, entry_context


    def analyze_symbol(self, symbol: str) -> Optional[Tuple[float, Dict, Dict, Dict, int, str]]:
        """[V5] 한 심볼에 대한 전체 분석을 수행하고 점수와 데이터를 반환합니다. (기존 analyze 메소드 역할)"""
        try:
            self._fetch_fear_and_greed_index()
            
            tf_scores: Dict[str, int] = {}
            tf_rows: Dict[str, pd.Series] = {}
            tf_score_breakdowns: Dict[str, Dict[str, int]] = {}
            timeframes = config.analysis_timeframes
            confluence_signal = ""

            for timeframe in timeframes:
                df = data_fetcher.fetch_klines(self.client, symbol, timeframe, limit=200)
                if df is None or df.empty:
                    tf_scores[timeframe], tf_score_breakdowns[timeframe] = 0, {}
                    continue
                
                indicators = indicator_calculator.calculate_all_indicators(df)
                if indicators is None or indicators.empty:
                    tf_scores[timeframe], tf_score_breakdowns[timeframe] = 0, {}
                    continue

                score, breakdown = self._calculate_tactical_score(indicators, timeframe)
                tf_scores[timeframe] = score
                tf_score_breakdowns[timeframe] = breakdown
                tf_rows[timeframe] = indicators.iloc[-1]
            
            final_score = 0.0
            for idx, tf in enumerate(timeframes):
                weight = config.tf_vote_weights[idx] if idx < len(config.tf_vote_weights) else 1.0
                final_score += tf_scores.get(tf, 0) * float(weight)

            if (tf_scores.get("4h", 0) > 0 and tf_scores.get("1d", 0) > 0) or \
               (tf_scores.get("4h", 0) < 0 and tf_scores.get("1d", 0) < 0):
                final_score *= 1.2
                confluence_signal = "📈 4h-1d 추세 동조!"
                print(confluence_signal)

            if self.fear_and_greed_index <= 25 and final_score > 0:
                final_score *= 1.2
                print("🥶 극심한 공포! 역추세 매수 기회 가중치 적용.")
            if self.fear_and_greed_index >= 75 and final_score < 0:
                final_score *= 1.2
                print("🤑 극심한 탐욕! 시장 과열 매도 신호 가중치 적용.")

            self._extract_legacy_data(tf_rows)

            return final_score, tf_scores, tf_rows, tf_score_breakdowns, self.fear_and_greed_index, confluence_signal
        
        except Exception as e:
            print(f"🚨 {symbol} 분석 중 심각한 오류 발생: {e}")
            return None

    # ==================================
    # 기존 헬퍼 함수들 (수정 없음)
    # ==================================
    def _fetch_fear_and_greed_index(self) -> None:
        try:
            response = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            response.raise_for_status()
            data = response.json()['data'][0]
            self.fear_and_greed_index = int(data['value'])
        except requests.RequestException:
            # 실패 시 조용히 이전 값을 사용
            pass

    def _find_rsi_divergence(self, df: pd.DataFrame, lookback: int = 14) -> int:
        if len(df) < lookback + 5: return 0
        recent_df = df.tail(lookback)
        lows, highs, rsi = recent_df['low'], recent_df['high'], recent_df['RSI_14']
        if lows.iloc[-1] < lows.iloc[0] and rsi.iloc[-1] > rsi.iloc[0]:
            if lows.idxmin() == lows.index[-1]: return 5
        if highs.iloc[-1] > highs.iloc[0] and rsi.iloc[-1] < rsi.iloc[0]:
            if highs.idxmax() == highs.index[-1]: return -5
        return 0

    def _calculate_tactical_score(self, df: pd.DataFrame, timeframe: str) -> Tuple[int, Dict[str, int]]:
        if df is None or df.empty or len(df) < 50: return 0, {}
        last = df.iloc[-1]
        scores = {"추세": 0, "자금": 0, "오실": 0, "다이버": 0, "BB": 0}
        
        # 추세 분석 (EMA)
        if all(k in df.columns for k in ["EMA_20", "EMA_50", "close"]):
            if last["close"] > last["EMA_20"] > last["EMA_50"]: scores["추세"] = 2
            elif last["close"] < last["EMA_20"] < last["EMA_50"]: scores["추세"] = -2
            elif last["close"] > last["EMA_50"]: scores["추세"] = 1
            elif last["close"] < last["EMA_50"]: scores["추세"] = -1
        
        # 자금 흐름 (MFI, OBV)
        if all(k in df.columns for k in ["MFI_14", "OBV"]):
            obv_ema = df['OBV'].ewm(span=20, adjust=False).mean().iloc[-1]
            if last["MFI_14"] > 80: scores["자금"] -= 1
            if last["MFI_14"] < 20: scores["자금"] += 1
            if last["OBV"] > obv_ema: scores["자금"] += 1
            if last["OBV"] < obv_ema: scores["자금"] -= 1
            
        # 오실레이터 (RSI, Stoch)
        if all(k in df.columns for k in ["RSI_14", "STOCHk_14_3_3"]):
            if last["RSI_14"] < 30 and last["STOCHk_14_3_3"] < 20: scores["오실"] = 2
            elif last["RSI_14"] > 70 and last["STOCHk_14_3_3"] > 80: scores["오실"] = -2
            elif last["RSI_14"] < 40: scores["오실"] = 1
            elif last["RSI_14"] > 60: scores["오실"] = -1

        # 다이버전스
        if "RSI_14" in df.columns: scores["다이버"] = self._find_rsi_divergence(df)

        # 볼린저 밴드 스퀴즈
        bbu_col = next((c for c in df.columns if c.startswith('BBU_')), None)
        bbl_col = next((c for c in df.columns if c.startswith('BBL_')), None)
        bbb_col = next((c for c in df.columns if c.startswith('BBB_')), None)
        if all([bbu_col, bbl_col, bbb_col]):
            is_squeeze = last[bbb_col] < df[bbb_col].rolling(90).quantile(0.05).iloc[-1]
            if is_squeeze and last['close'] > last[bbu_col]: scores["BB"] = 3
            elif is_squeeze and last['close'] < last[bbl_col]: scores["BB"] = -3
            
        return sum(scores.values()), scores

    def _extract_legacy_data(self, tf_rows: Dict[str, pd.Series]):
        """main.py 호환성을 위한 데이터 추가"""
        if "4h" in tf_rows:
            tf_rows["4h"]["adx_value"] = self._safe_number(tf_rows["4h"].get("ADX_14"))
        if "1d" in tf_rows:
            close = self._safe_number(tf_rows["1d"].get("close"))
            ema200 = self._safe_number(tf_rows["1d"].get("EMA_200"))
            if close and ema200:
                tf_rows["1d"]["is_above_ema200"] = close > ema200
    
    @staticmethod
    def _safe_number(val) -> Optional[float]:
        if isinstance(val, (int, float)) and not math.isnan(val): return float(val)
        return None

    def extract_atr(self, tf_rows: dict, primary_tf: str = "4h") -> float:
        row = tf_rows.get(primary_tf)
        if row is None: return 0.0
        for key in ("ATRr_14", "ATR_14"):
            val = self._safe_number(row.get(key))
            if val: return val
        return 0.0
