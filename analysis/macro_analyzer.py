# analysis/macro_analyzer.py (최종 안정화 버전)

from __future__ import annotations
import os
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
import pandas as pd
import yfinance as yf

try:
    from fredapi import Fred
except ImportError:
    Fred = None

class MacroRegime(Enum):
    BULL = "강세 국면 (Risk-On)"
    BEAR = "약세 국면 (Risk-Off)"
    SIDEWAYS = "중립/혼돈 국면"

class MacroAnalyzer:
    def __init__(self, config):
        self.config = config
        fred_key = getattr(self.config, 'fred_api_key', None) or os.getenv("FRED_API_KEY")
        print(f"🔑 FRED API Key Check: {'found' if fred_key else 'not found'}")
        self.fred = None
        if Fred and fred_key:
            try:
                self.fred = Fred(api_key=fred_key)
                print("✅ FRED API 초기화 완료.")
            except Exception as e:
                print(f"⚠️ FRED 초기화 실패: {e}")
        print("📈 MacroAnalyzer ready.")

    @staticmethod
    def _normalize_data(data):
        """데이터를 정규화하고, 정렬하며, 중복을 제거하는 가장 안정적인 방식"""
        if data is None or data.empty: return None
        out = data.copy()
        out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
        out = out.sort_index() # 시간순으로 강제 정렬
        if out.index.has_duplicates:
            out = out[~out.index.duplicated(keep='last')]
        out.index.name = "Date"
        return out

    def _get_yf(self, ticker: str, period: str = "5y", interval: str = "1d") -> Optional[pd.DataFrame]:
        try:
            df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False, timeout=10)
            return self._normalize_data(df)
        except Exception as e:
            print(f"🚨 yfinance 실패({ticker}): {e}")
            return None

    def _get_fred_series(self, series_id: str, start: str = "2000-01-01") -> Optional[pd.Series]:
        if self.fred is None: return None
        try:
            s = self.fred.get_series(series_id, start_date=start)
            return self._normalize_data(s)
        except Exception as e:
            print(f"🚨 FRED 조회 실패({series_id}): {e}")
            return None

    @staticmethod
    def _get_last_scalar(series: pd.Series) -> Optional[float]:
        """Pandas Series에서 마지막 단일 값(스칼라)을 가장 안전하게 추출합니다."""
        if series is None or series.empty:
            return None
        last_value = series.iloc[-1]
        if pd.notna(last_value):
            return float(last_value)
        return None

    # --- ▼▼▼ [핵심 수정] 모든 분석 함수를 새로운 안정적인 로직으로 재작성 ▼▼▼ ---

    def analyze_market_leader(self) -> Tuple[int, str]:
        data = self._get_yf("^IXIC")
        if data is None or len(data) < 200: return 0, "데이터 부족"
        data["SMA_200"] = data["Close"].rolling(200).mean()
        last_close = self._get_last_scalar(data["Close"])
        last_sma = self._get_last_scalar(data["SMA_200"])
        if last_close is not None and last_sma is not None:
            return (5, "강세") if last_close > last_sma else (-5, "약세")
        return 0, "데이터 불완전"

    def analyze_market_volatility(self) -> Tuple[int, str]:
        data = self._get_yf("^VIX")
        if data is None or len(data) < 20: return 0, "데이터 부족"
        data["SMA_20"] = data["Close"].rolling(20).mean()
        last_close = self._get_last_scalar(data["Close"])
        last_sma = self._get_last_scalar(data["SMA_20"])
        if last_close is not None and last_sma is not None:
            if last_close > 30: return -5, "극심한 공포"
            if last_close > 20 and last_close > last_sma: return -3, "공포 확산"
            if last_close < 15: return 3, "시장 안정"
            return 0, "중립"
        return 0, "데이터 불완전"

    def analyze_credit_risk(self) -> Tuple[int, str]:
        s = self._get_fred_series("BAMLH0A0HYM2")
        if s is None or len(s) < 50: return 0, "데이터 부족"
        sma50 = s.rolling(50).mean()
        last_val = self._get_last_scalar(s)
        last_sma = self._get_last_scalar(sma50)
        if last_val is not None and last_sma is not None:
             return (-5, "신용 경색") if last_val > last_sma else (3, "자금 원활")
        return 0, "데이터 불완전"

    def analyze_liquidity(self) -> Tuple[int, str]:
        data = self._get_yf("DX-Y.NYB")
        if data is None or len(data) < 50: data = self._get_yf("UUP")
        if data is None or len(data) < 50: return 0, "데이터 부족"
        data["SMA_50"] = data["Close"].rolling(50).mean()
        last_close = self._get_last_scalar(data["Close"])
        last_sma = self._get_last_scalar(data["SMA_50"])
        if last_close is not None and last_sma is not None:
            return (4, "달러 약세") if last_close < last_sma else (-4, "달러 강세")
        return 0, "데이터 불완전"

    def analyze_inflation_proxy(self) -> Tuple[int, str]:
        data = self._get_yf("CL=F")
        if data is None or len(data) < 50: return 0, "데이터 부족"
        data["SMA_50"] = data["Close"].rolling(50).mean()
        last_close = self._get_last_scalar(data["Close"])
        last_sma = self._get_last_scalar(data["SMA_50"])
        if last_close is not None and last_sma is not None:
            return (2, "상승") if last_close > last_sma else (-2, "하락")
        return 0, "데이터 불완전"

    def analyze_yield_curve(self) -> Tuple[int, str]:
        s = self._get_fred_series("T10Y2Y")
        if s is None or s.empty: return 0, "데이터 부족"
        val = self._get_last_scalar(s)
        if val is not None:
            if val < 0: return -7, "금리 역전 (침체 신호)"
            if val < 0.25: return -3, "금리차 축소 (위험)"
            return 2, "정상"
        return 0, "데이터 불완전"

    def diagnose_macro_regime(self) -> Tuple[MacroRegime, int, Dict[str, int]]:
        scores = {
            "주도주(나스닥)": self.analyze_market_leader()[0],
            "변동성(VIX)": self.analyze_market_volatility()[0],
            "신용위험(회사채)": self.analyze_credit_risk()[0],
            "유동성(달러)": self.analyze_liquidity()[0],
            "인플레이션(유가)": self.analyze_inflation_proxy()[0],
            "경기침체(금리차)": self.analyze_yield_curve()[0],
        }
        final = sum(scores.values())
        if final >= 7: return MacroRegime.BULL, final, scores
        if final <= -7: return MacroRegime.BEAR, final, scores
        return MacroRegime.SIDEWAYS, final, scores
    # (preload_all_macro_data, diagnose_macro_regime_for_date 등 나머지 함수는 변경 없음)
    # (preload_all_macro_data, diagnose_macro_regime_for_date 등 나머지 함수는 변경 없음)
    # -------------------- 프리로드(세그멘트용) --------------------
    def preload_all_macro_data(self) -> Dict[str, Any]:
        """
        NASDAQ, VIX는 항상 프리로드(일자 인덱스).
        FRED 키가 있으면 HY 스프레드/T10Y2Y도 추가.
        """
        print("… 거시 데이터 프리로드 중 …")
        nasdaq = self._get_yf("^IXIC")
        if nasdaq is not None:
            nasdaq["SMA_200"] = nasdaq["Close"].rolling(200).mean()
        vix = self._get_yf("^VIX")
        if vix is not None:
            vix["SMA_20"] = vix["Close"].rolling(20).mean()

        out: Dict[str, Any] = {"nasdaq": nasdaq, "vix": vix}

        if self.fred is not None:
            hy = self._get_fred_series("BAMLH0A0HYM2")
            yc = self._get_fred_series("T10Y2Y")
            if hy is not None:
                out["hy_spread"] = hy
            if yc is not None:
                out["t10y2y"] = yc
        return out

    # -------------------- 날짜별 진단(세그멘트 핵심) --------------------
    def diagnose_macro_regime_for_date(self, analysis_date, macro_data: dict) -> Tuple[MacroRegime, int, Dict[str, int]]:
        """
        4H 캔들 타임스탬프 → '일자'로 내리고, 그 날짜 '이하'의 마지막 값으로 판단.
        (인덱스는 이미 normalize 되어 있으므로 단순하고 빠름)
        """
        day = pd.Timestamp(analysis_date).tz_localize(None).normalize()

        def last_le(df_or_s, d):
            if df_or_s is None or len(df_or_s) == 0:
                return None
            try:
                return df_or_s.loc[:d].iloc[-1]
            except Exception:
                return None

        scores: Dict[str, int] = {}

        nd = macro_data.get("nasdaq")
        nd_row = last_le(nd, day)
        scores["주도주(나스닥)"] = 0
        if isinstance(nd_row, pd.Series) and {"Close", "SMA_200"}.issubset(nd_row.index):
            scores["주도주(나스닥)"] = 5 if nd_row["Close"] > nd_row["SMA_200"] else -5

        vx = macro_data.get("vix")
        vx_row = last_le(vx, day)
        scores["변동성(VIX)"] = 0
        if isinstance(vx_row, pd.Series) and {"Close", "SMA_20"}.issubset(vx_row.index):
            if vx_row["Close"] > 30:
                scores["변동성(VIX)"] = -5
            elif vx_row["Close"] > 20 and vx_row["Close"] > vx_row["SMA_20"]:
                scores["변동성(VIX)"] = -3
            elif vx_row["Close"] < 15:
                scores["변동성(VIX)"] = 3

        hy = macro_data.get("hy_spread")
        if isinstance(hy, pd.Series) and len(hy) >= 50:
            hy_row = last_le(hy, day)
            if hy_row is not None:
                hy50 = hy.loc[:day].rolling(50).mean().iloc[-1]
                scores["신용위험(회사채)"] = -5 if float(hy_row) > float(hy50) else 3

        yc = macro_data.get("t10y2y")
        if isinstance(yc, pd.Series) and len(yc) > 0:
            yc_row = last_le(yc, day)
            if yc_row is not None:
                val = float(yc_row)
                if val < 0:
                    scores["경기침체(금리차)"] = -7
                elif val < 0.25:
                    scores["경기침체(금리차)"] = -3
                else:
                    scores["경기침체(금리차)"] = 2

        total = sum(scores.values())
        if total >= 5:
            return MacroRegime.BULL, total, scores
        if total <= -5:
            return MacroRegime.BEAR, total, scores
        return MacroRegime.SIDEWAYS, total, scores
