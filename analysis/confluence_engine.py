# 파일명: analysis/confluence_engine.py (V4 업그레이드)
# 'Executive's Brain' 업그레이드의 핵심. 시장의 거시/미시 환경과 심리를 복합적으로 분석.

from __future__ import annotations
import math
from typing import Dict, Tuple, Mapping, Optional
import pandas as pd
from binance.client import Client
import requests # V4: 공포-탐욕 지수 API 연동을 위해 추가

from core.config_manager import config
from . import data_fetcher, indicator_calculator

class ConfluenceEngine:
    """
    [V4] Macro-Tactical Confluence Engine.
    시장의 거시 환경(날씨), 참여자 심리(온도), 그리고 미시적 타점(전술)을 계층적으로 분석하여
    'A급 타점'을 식별하고 점수화합니다.
    """
    def __init__(self, client: Client):
        self.client = client
        self.fear_and_greed_index = 50 # 기본값: 중립
        print("📈 [V4] Macro-Tactical 컨플루언스 엔진이 초기화되었습니다.")

    # ==================================
    # 🚀 1단계: 시장의 '날씨'와 '심리' 분석 - Macro Analysis Layer
    # ==================================
    def _fetch_fear_and_greed_index(self) -> None:
        """외부 API를 통해 공포-탐욕 지수를 가져와 업데이트합니다."""
        try:
            # 하루에 한 번만 호출해도 충분하지만, 데모를 위해 매 분석 시 호출
            response = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            response.raise_for_status()
            data = response.json()['data'][0]
            self.fear_and_greed_index = int(data['value'])
            print(f"🧠 공포-탐욕 지수 업데이트: {self.fear_and_greed_index} ({data['value_classification']})")
        except requests.RequestException as e:
            print(f"⚠️ 공포-탐욕 지수 API 호출 실패: {e}. 이전 값({self.fear_and_greed_index})을 사용합니다.")

    # ==================================
    # 🚀 2단계: 'A급 타점' 식별 - Tactical Entry Layer
    # ==================================
    def _find_rsi_divergence(self, df: pd.DataFrame, lookback: int = 14) -> int:
        """RSI 다이버전스를 탐지하여 강력한 추세 전환 신호에 높은 점수를 부여합니다."""
        if len(df) < lookback + 5: return 0
        
        recent_df = df.tail(lookback)
        lows = recent_df['low']
        highs = recent_df['high']
        rsi = recent_df['RSI_14']

        # 강세 다이버전스: 가격은 저점을 낮추는데, RSI는 저점을 높임
        if lows.iloc[-1] < lows.iloc[0] and rsi.iloc[-1] > rsi.iloc[0]:
            # 더 명확한 신호를 위해, 최근 5개 봉 중 가장 낮은 저점과 비교
            if lows.idxmin() == lows.index[-1]:
                print(f"💎 강세 다이버전스 발견!")
                return 5 # 매우 높은 가산점

        # 약세 다이버전스: 가격은 고점을 높이는데, RSI는 고점을 낮춤
        if highs.iloc[-1] > highs.iloc[0] and rsi.iloc[-1] < rsi.iloc[0]:
            if highs.idxmax() == highs.index[-1]:
                print(f" Alerts  약세 다이버전스 발견!")
                return -5 # 매우 높은 감점

        return 0

    def _calculate_tactical_score(self, df: pd.DataFrame, timeframe: str) -> int:
        """[V4 핵심] 지정된 타임프레임의 기술적 지표를 복합적으로 분석하여 전술적 점수를 계산합니다."""
        if df is None or df.empty or len(df) < 50:
            print(f"[{timeframe}] 데이터 부족으로 전술 분석 건너뜀 (데이터 수: {0 if df is None else len(df)})")
            return 0

        score = 0
        last = df.iloc[-1]
        
        # --- 1. 자금 흐름(Money Flow) 분석 ---
        mfi = self._safe_number(last.get("MFI_14"))
        obv = self._safe_number(last.get("OBV"))
        obv_ema = self._safe_number(df['OBV'].ewm(span=20, adjust=False).mean().iloc[-1])
        
        money_flow_score = 0
        if mfi is not None and obv is not None and obv_ema is not None:
            if mfi > 80: money_flow_score -= 1 # 과매수
            if mfi < 20: money_flow_score += 1 # 과매도
            if obv > obv_ema: money_flow_score += 1 # 매집 우위
            if obv < obv_ema: money_flow_score -= 1 # 분산 우위

        # --- 2. 오실레이터 교차 확인 (RSI + Stochastic) ---
        rsi = self._safe_number(last.get("RSI_14"))
        stoch_k = self._safe_number(last.get("STOCHk_14_3_3"))
        
        oscillator_score = 0
        if rsi is not None and stoch_k is not None:
            if rsi < 30 and stoch_k < 20: oscillator_score = 2   # 동시 과매도 (강력 매수)
            elif rsi > 70 and stoch_k > 80: oscillator_score = -2  # 동시 과매수 (강력 매도)
            elif rsi < 40: oscillator_score = 1
            elif rsi > 60: oscillator_score = -1

        # --- 3. 다이버전스 자동 탐지 (가장 높은 가중치) ---
        divergence_score = self._find_rsi_divergence(df)

        # --- 4. 볼린저 밴드 스퀴즈 후 돌파 ---
        bb_squeeze_score = 0
        bbw = df['BBP_20_2.0'] # pandas-ta의 bbands()는 BBP 컬럼을 제공
        if not bbw.empty:
            # 최근 20개 캔들 중 BBW가 최저치 근처에 있다가(스퀴즈), 최근 캔들이 밴드를 돌파
            is_squeeze = bbw.iloc[-5:-1].min() < 0.3
            is_breakout_up = last['close'] > last['BBU_20_2.0']
            is_breakout_down = last['close'] < last['BBL_20_2.0']
            
            if is_squeeze and is_breakout_up:
                bb_squeeze_score = 3
                print("🔥 볼린저 밴드 상방 돌파!")
            elif is_squeeze and is_breakout_down:
                bb_squeeze_score = -3
                print("🧊 볼린저 밴드 하방 돌파!")

        # --- 5. 기존 추세 분석 (EMA 기반) ---
        ema20 = self._safe_number(last.get("EMA_20"))
        ema50 = self._safe_number(last.get("EMA_50"))
        close = self._safe_number(last.get("close"))
        
        trend_score = 0
        if all(v is not None for v in [close, ema20, ema50]):
            if close > ema20 > ema50: trend_score = 2  # 정배열 강세
            elif close < ema20 < ema50: trend_score = -2 # 역배열 약세
            elif close > ema50: trend_score = 1
            elif close < ema50: trend_score = -1
            
        score = trend_score + money_flow_score + oscillator_score + divergence_score + bb_squeeze_score
        print(f"[{timeframe}] 전술 점수: 추세({trend_score}) + 자금({money_flow_score}) + 오실({oscillator_score}) + 다이버({divergence_score}) + BB({bb_squeeze_score}) -> 합계: {score}")
        return score

    # ==================================
    # 🚀 3단계: 최종 판단 - Confluence Layer
    # ==================================
    def analyze(self, symbol: str) -> Tuple[float, Dict[str, int], Dict[str, pd.Series]]:
        """[V4] 시장의 모든 요소를 종합하여 최종 컨플루언스 점수를 계산합니다."""
        # 1. 거시 심리 분석 (API 호출)
        self._fetch_fear_and_greed_index()
        
        tf_scores: Dict[str, int] = {}
        tf_rows: Dict[str, pd.Series] = {}
        timeframes = config.analysis_timeframes

        for timeframe in timeframes:
            df = data_fetcher.fetch_klines(self.client, symbol, timeframe, limit=200) # 다이버전스 계산 위해 데이터 증가
            if df is None or df.empty:
                tf_scores[timeframe] = 0
                continue
            
            indicators = indicator_calculator.calculate_all_indicators(df)
            if indicators is None or indicators.empty:
                tf_scores[timeframe] = 0
                continue
            
            tf_scores[timeframe] = self._calculate_tactical_score(indicators, timeframe)
            tf_rows[timeframe] = indicators.iloc[-1]

        # 2. 최종 점수 집계 (가중 투표 + 심리 지수 반영)
        final_score = 0.0
        for idx, timeframe in enumerate(timeframes):
            weight = config.tf_vote_weights[idx] if idx < len(config.tf_vote_weights) else 1.0
            final_score += tf_scores.get(timeframe, 0) * float(weight)

        # 3. 거시-미시 동조화 가중
        # 4h와 1d의 방향성이 같으면 신뢰도 상승
        if (tf_scores.get("4h", 0) > 0 and tf_scores.get("1d", 0) > 0) or \
           (tf_scores.get("4h", 0) < 0 and tf_scores.get("1d", 0) < 0):
            final_score *= 1.2
            print("📈 4h-1d 추세 동조! 신뢰도 가중치 적용.")

        # 4. 시장 심리 반영
        # 극단적 공포 상태에서 매수 신호가 나오면 가산점, 탐욕 상태에서 매도 신호가 나오면 가산점
        if self.fear_and_greed_index <= 25 and final_score > 0:
            final_score *= 1.2
            print("🥶 극심한 공포! 역추세 매수 기회일 수 있습니다. 가중치 적용.")
        if self.fear_and_greed_index >= 75 and final_score < 0:
            final_score *= 1.2
            print("🤑 극심한 탐욕! 시장 과열 가능성. 매도 신호에 가중치 적용.")

        # V3 호환성을 위한 추가 데이터 추출 (main.py에서 사용)
        self._extract_legacy_data(tf_rows)

        return final_score, tf_scores, tf_rows
    
    # --- 유틸리티 및 하위 호환성 함수들 (기존과 거의 동일) ---
    
    def _extract_legacy_data(self, tf_rows: Dict[str, pd.Series]):
        """main.py의 V3 로직이 V4 데이터 구조와 호환되도록 데이터를 추가합니다."""
        four_hour_row = tf_rows.get("4h")
        if isinstance(four_hour_row, pd.Series):
            adx_value = four_hour_row.get(f"ADX_14")
            sanitized_adx = self._safe_number(adx_value)
            # copy()를 사용하여 원본 데이터 변경 방지
            updated = four_hour_row.copy()
            updated["adx_value"] = sanitized_adx
            tf_rows["4h"] = updated

        daily_row = tf_rows.get("1d")
        if isinstance(daily_row, pd.Series):
            updated_daily = daily_row.copy()
            close_price = self._safe_number(updated_daily.get("close"))
            ema200 = self._safe_number(updated_daily.get("EMA_200"))
            if close_price is not None and ema200 is not None:
                updated_daily["is_above_ema200"] = close_price > ema200
            tf_rows["1d"] = updated_daily
    
    @staticmethod
    def _safe_number(val) -> Optional[float]:
        if isinstance(val, (int, float)) and not math.isnan(val):
            return float(val)
        try:
            f = float(val)
            return f if not math.isnan(f) else None
        except (ValueError, TypeError):
            return None

    def extract_atr(self, tf_rows: Mapping, primary_tf: str = "4h") -> float:
        # 이 함수는 V4에서 직접 사용되진 않지만, 외부(main.py) 호환성을 위해 유지합니다.
        row = tf_rows.get(primary_tf)
        if row is None: return 0.0
        
        for key in ("ATRr_14", "ATR_14"):
            val = self._safe_number(row.get(key))
            if val is not None:
                return val
        return 0.0
