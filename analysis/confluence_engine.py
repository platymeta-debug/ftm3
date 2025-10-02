# 파일명: analysis/confluence_engine.py
# (전체 최종 수정안)

"""Hierarchical confluence engine responsible for scoring market bias."""

from __future__ import annotations

import math
from typing import Dict, Tuple, Mapping, Optional

import pandas as pd
from binance.client import Client
import pandas_ta as ta

from core.config_manager import config
from . import data_fetcher, indicator_calculator  # ← 점(.) 누락 버그 수정


class ConfluenceEngine:
    """Combine multi-timeframe indicators into a single confluence score."""

    def __init__(self, client: Client):
        self.client = client
        print("계층적 컨플루언스 엔진이 초기화되었습니다.")

    # =========================
    # 내부 유틸 (정규화/안전장치)
    # =========================
    @staticmethod
    def _to_scalar_tf(tf_val) -> Optional[str]:
        """'4h' | ['4h'] | ('4h',) | None -> '4h' or None"""
        if tf_val is None:
            return None
        if isinstance(tf_val, (list, tuple)):
            if not tf_val:
                return None
            tf_val = tf_val[0]
        # 숫자 등도 들어올 수 있으므로 문자열로 보정
        return str(tf_val)

    @staticmethod
    def _normalize_tf_rows(tf_rows: Mapping) -> Dict[str, pd.Series]:
        """tf_rows의 키가 리스트/튜플이어도 문자열 스칼라 키로 정규화."""
        if not isinstance(tf_rows, Mapping):
            raise TypeError(f"tf_rows는 dict(Mapping)이어야 합니다. got: {type(tf_rows).__name__}")
        fixed: Dict[str, pd.Series] = {}
        for k, v in tf_rows.items():
            nk = ConfluenceEngine._to_scalar_tf(k)
            if nk is None:
                # 키가 None이면 스킵
                continue
            fixed[nk] = v
        return fixed

    @staticmethod
    def _safe_number(val) -> Optional[float]:
        """숫자이면 float로, NaN이면 None, 그 외는 None."""
        if isinstance(val, (int, float)):
            if math.isnan(val):
                return None
            return float(val)
        # numpy/pandas 스칼라 호환
        try:
            f = float(val)
            if math.isnan(f):
                return None
            return f
        except Exception:
            return None

    # =========================
    # 스코어 계산
    # =========================
    def _calculate_bias_score(self, df: pd.DataFrame, timeframe: str) -> int:
        if df is None or df.empty or len(df) < 200:
            print(f"[{timeframe}] 데이터 부족으로 점수 계산 건너뜀 (데이터 수: {0 if df is None else len(df)})")
            return 0

        score = 0
        last_row = df.iloc[-1]

        close_price = self._safe_number(last_row.get("close"))
        ema20 = self._safe_number(last_row.get("EMA_20"))
        ema50 = self._safe_number(last_row.get("EMA_50"))
        ema200 = self._safe_number(last_row.get("EMA_200"))
        rsi_value = self._safe_number(last_row.get("RSI_14"))
        tenkan_sen = self._safe_number(last_row.get("ITS_9"))
        kijun_sen = self._safe_number(last_row.get("IKS_26"))
        senkou_a = self._safe_number(last_row.get("ISA_9"))
        senkou_b = self._safe_number(last_row.get("ISB_26"))

        def f(val):
            return f"{val:.2f}" if isinstance(val, (int, float)) else ("N/A" if val is None else str(val))

        print(f"--- [{timeframe}] 지표 값 ---")
        print(f"Close: {f(close_price)}, EMA20: {f(ema20)}, EMA50: {f(ema50)}, EMA200: {f(ema200)}")
        print(f"RSI: {f(rsi_value)}, Tenkan: {f(tenkan_sen)}, Kijun: {f(kijun_sen)}, SpanA: {f(senkou_a)}, SpanB: {f(senkou_b)}")

        trend_score, rsi_score, ichimoku_score = 0, 0, 0

        # Trend
        if all(v is not None for v in [close_price, ema20, ema50, ema200]):
            if ema20 > ema50 > ema200:
                trend_score = 2
            elif ema20 < ema50 < ema200:
                trend_score = -2
            elif close_price > ema50:
                trend_score = 1
            elif close_price < ema50:
                trend_score = -1

        # RSI
        if rsi_value is not None:
            if rsi_value > 70:
                rsi_score = -1
            elif rsi_value < 30:
                rsi_score = 1

        # Ichimoku
        if all(v is not None for v in [close_price, tenkan_sen, kijun_sen, senkou_a, senkou_b]):
            if close_price > senkou_a and close_price > senkou_b:
                ichimoku_score = 2 if tenkan_sen > kijun_sen else 1
            elif close_price < senkou_a and close_price < senkou_b:
                ichimoku_score = -2 if tenkan_sen < kijun_sen else -1

        score = trend_score + rsi_score + ichimoku_score
        print(f"점수 계산: Trend({trend_score}), RSI({rsi_score}), Ichimoku({ichimoku_score}) -> 합계: {score}")
        return score

    # =========================
    # 메인 분석
    # =========================
    def analyze(self, symbol: str) -> Tuple[float, Dict[str, int], Dict[str, pd.Series]]:
        tf_scores: Dict[str, int] = {}
        tf_rows: Dict[str, pd.Series] = {}

        # config.analysis_timeframes 가 리스트/튜플 전제. (문자열 1개만 올 가능성도 대비)
        timeframes = config.analysis_timeframes
        if isinstance(timeframes, (str,)):
            timeframes = [timeframes]
        elif not isinstance(timeframes, (list, tuple)):
            # 안전장치
            timeframes = list(timeframes) if timeframes is not None else []

        for timeframe in timeframes:
            tf_str = self._to_scalar_tf(timeframe)  # 혹시 리스트/튜플로 들어오면 보정
            if not tf_str:
                continue

            df = data_fetcher.fetch_klines(self.client, symbol, tf_str)
            if df is None or df.empty:
                tf_scores[tf_str] = 0
                continue

            indicators = indicator_calculator.calculate_all_indicators(df)
            if indicators is None or indicators.empty:
                tf_scores[tf_str] = 0
                continue

            tf_scores[tf_str] = self._calculate_bias_score(indicators, tf_str)
            tf_rows[tf_str] = indicators.iloc[-1]

        # --- [Milestone 3] 추가 분석 데이터 추출 ---
        four_hour_row = tf_rows.get("4h")
        if isinstance(four_hour_row, pd.Series):
            adx_value = four_hour_row.get(f"ADX_{ta.ADX_LENGTH}")
            sanitized_adx = self._safe_number(adx_value)
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
        # --- [Milestone 3] 추가 분석 데이터 추출 ---

        # 가중 투표
        final_score = 0.0
        for idx, timeframe in enumerate(timeframes):
            tf_str = self._to_scalar_tf(timeframe)
            if not tf_str:
                continue
            weight = (
                config.tf_vote_weights[idx]
                if hasattr(config, "tf_vote_weights") and idx < len(getattr(config, "tf_vote_weights", []))
                else 1.0
            )
            final_score += tf_scores.get(tf_str, 0) * float(weight)

        # 4h & 1d 동조 가중(있으면)
        if (tf_scores.get("4h", 0) > 0 and tf_scores.get("1d", 0) > 0) or (
            tf_scores.get("4h", 0) < 0 and tf_scores.get("1d", 0) < 0
        ):
            final_score *= 1.2

        return final_score, tf_scores, tf_rows

    # =========================
    # ATR 추출 (버그 수정 핵심)
    # =========================
    def extract_atr(
        self,
        tf_rows: Mapping,
        primary_tf: Optional[str | list | tuple] = None,
        fallback_order: tuple[str, ...] = ("4h", "1h", "30m", "15m", "5m"),
    ) -> float:
        """
        tf_rows에서 ATR 값을 추출한다.

        - primary_tf 가 리스트/튜플이어도 안전하게 첫 요소로 정규화한다.
        - primary_tf 가 없거나 해당 키가 없으면 fallback_order 순서대로 시도.
        - tf_rows 키가 리스트/튜플로 잘못 들어와도 문자열 스칼라로 정규화한다.
        """
        if not tf_rows:
            return 0.0

        # 키 정규화 (리스트 키 → 문자열 키)
        tf_rows_norm = self._normalize_tf_rows(tf_rows)

        # primary_tf 우선: 명시 인자 → config.analysis_timeframes[0]
        tf_choice = self._to_scalar_tf(primary_tf)
        if not tf_choice:
            # config 기반 1순위 사용
            tfs = config.analysis_timeframes
            if isinstance(tfs, (str,)):
                tf_choice = tfs
            elif isinstance(tfs, (list, tuple)) and tfs:
                tf_choice = self._to_scalar_tf(tfs[0])
            else:
                tf_choice = None

        # primary 후보가 tf_rows에 없다면 fallback 사용
        if not tf_choice or tf_choice not in tf_rows_norm:
            for cand in fallback_order:
                if cand in tf_rows_norm:
                    tf_choice = cand
                    break

        # 그래도 없으면 사용 가능한 첫 키 사용
        if not tf_choice:
            if not tf_rows_norm:
                return 0.0
            # 키 하나 뽑기
            tf_choice = next(iter(tf_rows_norm.keys()))

        row = tf_rows_norm.get(tf_choice)
        if row is None:
            return 0.0

        # 지원 키 우선순위
        for key in ("ATR_14", "ATRr_14", "atr_14", "atr"):
            value = row.get(key) if isinstance(row, Mapping) else (row[key] if key in row.index else None)
            val = self._safe_number(value)
            if val is not None:
                return float(val)

        return 0.0
