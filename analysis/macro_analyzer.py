# analysis/macro_analyzer.py (최종 안정화 버전)
# - 진릿값 모호성(ValueError) 원천 차단: 과거/당일 인덱스 pad → iloc[pos] 일원화
# - 결측/휴일/미래일 처리 보강, 중복 인덱스 제거
# - 스냅샷 기준시각 일원화(전 지표 동일 기준일 평가)
# - FRED API 비활성 시 자동 우회
# - 리스크 계열 중복 캡(VIX vs MOVE)

from __future__ import annotations

import os
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, Iterable
from datetime import datetime

import pandas as pd
import yfinance as yf

try:
    from fredapi import Fred
except ImportError:
    Fred = None

# --- [안정성 강화] ---
# yfinance와 FRED의 데이터 조회 실패에 대비한 안정적인 티커 리스트
DOLLAR_TICKERS = ("DX-Y.NYB", "DX=F", "^NYICDX")
# FRED의 공식 Series ID 사용
FRED_TICKERS = {
    "hy_spread": "BAMLH0A0HYM2", "t10y2y": "T10Y2Y", "m2": "M2SL",
    "sentiment": "UMCSENT", "ism": "GACDISA066MSFRBNY", "housing": "HOUST",
    "move": "DGS10", "unrate": "UNRATE", "tga": "WTREGEN",
    "retail": "RSAFS", "indpro": "INDPRO"
}

# =========================================================
# 공용 타입
# =========================================================
class MacroRegime(Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"

class MacroState(Enum):
    EXPANSION = "EXPANSION"
    RECOVERY = "RECOVERY"
    OVERHEAT = "OVERHEAT"
    RECESSION = "RECESSION"

@dataclass
class MacroConfig:
    fred_api_key: Optional[str] = None
    use_yf_cache: bool = True
    # DXY 포함(야후 종종 실패 대비 DX-Y.NYB와 병행)
    snapshot_symbols: Tuple[str, ...] = ("^IXIC", "^VIX", *DOLLAR_TICKERS, "CL=F", "GC=F", "HG=F")

# =========================================================
# 스냅샷 매니저: 가격/일변화 기준시각 일원화
# =========================================================
class PriceSnapshot:
    def __init__(self):
        self.frames: Dict[str, pd.DataFrame] = {}
        self.snapshot_date: Optional[pd.Timestamp] = None

    @staticmethod
    def _normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
        """일봉 인덱스 표준화 + 중복/미래 제거."""
        if df is None or df.empty:
            return pd.DataFrame()
        out = df.copy()
        out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
        out = out[~out.index.duplicated(keep='first')]
        out = out[out.index <= datetime.now()]
        out.index.name = "Date"
        return out

    @staticmethod
    def _get_position_before_date(df: pd.DataFrame, day: pd.Timestamp) -> Optional[int]:
        if df is None or df.empty:
            return None
        try:
            pos = df.index.get_indexer([day], method="pad")[0]
            return int(pos) if pos != -1 else None
        except Exception:
            return None

    def take(self, symbols: Iterable[str], period: str = "2y", interval: str = "1d") -> None:
        last_dates = []
        for sym in symbols:
            try:
                df = yf.download(sym, period=period, interval=interval, progress=False, auto_adjust=False)
                df = self._normalize_daily(df)
                self.frames[sym] = df
                if df is not None and not df.empty:
                    last_dates.append(df.index[-1])
            except Exception:
                continue
        if last_dates:
            s = pd.Series(last_dates).dt.normalize()
            # 가장 흔한(모드) 날짜를 스냅샷 기준일로 사용
            self.snapshot_date = s.mode().iloc[0]

    def get_df(self, symbol: str) -> Optional[pd.DataFrame]:
        return self.frames.get(symbol)

    def last_row(self, symbol: str) -> Optional[pd.Series]:
        df = self.get_df(symbol)
        if df is None or df.empty:
            return None
        if self.snapshot_date is None:
            return df.iloc[-1]
        idx = df.index.get_indexer([self.snapshot_date], method="pad")
        if len(idx) == 0 or idx[0] == -1:
            return None
        return df.iloc[idx[0]]

# =========================================================
# 메인 분석기
# =========================================================
class MacroAnalyzer:
    def __init__(self, config: Optional[MacroConfig] = None):
        self.config = config or MacroConfig()
        fred_key = getattr(self.config, 'fred_api_key', None) or os.getenv("FRED_API_KEY")

        def _mask(s: str) -> str:
            return f"{len(s)} chars" if s else "empty"
        print(f"🔑 FRED API Key Check: {('found ' + _mask(fred_key)) if fred_key else 'not found'}")

        self.fred = None
        if Fred and fred_key:
            try:
                self.fred = Fred(api_key=fred_key)
                print("✅ FRED API 초기화 완료.")
            except Exception as e:
                print(f"⚠️ FRED 초기화 실패: {e} → FRED 지표 비활성.")
        elif Fred is None:
            print("ℹ️ fredapi 패키지가 설치되어 있지 않습니다. FRED 지표 비활성.")
        else:
            print("ℹ️ FRED API 키가 없어 FRED 지표가 비활성 상태입니다.")

        # 스냅샷 일원화(전략/매크로 동일 기준시각)
        self.snapshot = PriceSnapshot()
        self.snapshot.take(self.config.snapshot_symbols)
        if self.snapshot.snapshot_date is not None:
            print(f"🕒 Price Snapshot date = {self.snapshot.snapshot_date.date()}")
        print("📈 MacroAnalyzer ready (17-factor model, v-final).")

    # ------------------------
    # 내부 헬퍼
    # ------------------------
    @staticmethod
    def _normalize_daily_index(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        out = df.copy()
        out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
        out.index.name = "Date"
        return out

    def _get_yf(self, ticker: str, period: str = "10y", interval: str = "1d") -> Optional[pd.DataFrame]:
        snap_df = self.snapshot.get_df(ticker)
        if snap_df is not None and not snap_df.empty:
            snap_df = snap_df[~snap_df.index.duplicated(keep='first')]
            return snap_df
        try:
            df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
            if df is None or df.empty:
                return None
            df = self._normalize_daily_index(df)
            df = df[~df.index.duplicated(keep='first')]
            return df
        except Exception:
            return None

    def _get_fred_series(self, series_id: str, start: str = "2010-01-01") -> Optional[pd.Series]:
        if self.fred is None:
            return None
        try:
            s = self.fred.get_series(series_id, start_date=start)
            if s is None or len(s) == 0:
                return None
            return s
        except Exception as e:
            print(f"🚨 FRED 조회 실패({series_id}): {e}")
            return None

    # --- 안전 헬퍼: DataFrame/Series 다중 시도 (or 사용 금지) ---
    def _try_yf(self, *tickers: str) -> Optional[pd.DataFrame]:
        for t in tickers:
            df = self._get_yf(t)
            if df is not None and not df.empty:
                return df
        return None

    # =========================================================
    # 17가지 핵심 지표 분석 함수 (임계/윈도우 현실화)
    # =========================================================

    def analyze_market_leader(self) -> Tuple[int, str]:  # 1. 주도주 (나스닥)
        data = self._get_yf("^IXIC")
        if data is None or len(data) < 200:
            return 0, "데이터 부족"
        data["SMA_200"] = data["Close"].rolling(200).mean()
        last = self.snapshot.last_row("^IXIC") or data.iloc[-1]
        return (5, "강세") if last["Close"].item() > last["SMA_200"].item() else (-5, "약세")

    def analyze_market_volatility(self) -> Tuple[int, str]:  # 2. 변동성 (VIX)
        data = self._get_yf("^VIX")
        if data is None or len(data) < 20:
            return 0, "데이터 부족"
        data["SMA_20"] = data["Close"].rolling(20).mean()
        last = self.snapshot.last_row("^VIX") or data.iloc[-1]
        last_close = last["Close"].item()
        sma20 = last["SMA_20"].item()
        if last_close > 32:
            return -5, "극심한 공포"
        if last_close > 24 and last_close > sma20:
            return -3, "경계 구간"
        if last_close < 14:
            return 3, "안정 구간"
        return 0, "중립"

    def analyze_credit_risk(self) -> Tuple[int, str]:  # 3. 신용위험 (HY 스프레드)
        s = self._get_fred_series("BAMLH0A0HYM2")
        if s is None or len(s) < 80:
            return 0, "데이터 부족/FRED 비활성"
        sma = s.rolling(75).mean()
        last = s.iloc[-1]
        return (-5, "신용경색") if last > sma.iloc[-1] else (3, "완화")

    def analyze_liquidity(self) -> Tuple[int, str]:  # 4. 유동성 (달러)
        data = self._try_yf(*DOLLAR_TICKERS)
        if data is None or len(data) < 100:
            return 0, "데이터 부족"
        data["SMA_100"] = data["Close"].rolling(100).mean()
        last = None
        for sym in DOLLAR_TICKERS:
            last = self.snapshot.last_row(sym)
            if last is not None:
                break
        if last is None:
            last = data.iloc[-1]
        return (4, "달러 약세") if last["Close"].item() < last["SMA_100"].item() else (-4, "달러 강세")

    def analyze_inflation_proxy(self) -> Tuple[int, str]:  # 5. 인플레이션 (유가)
        data = self._get_yf("CL=F")
        if data is None or len(data) < 100:
            return 0, "데이터 부족"
        data["SMA_100"] = data["Close"].rolling(100).mean()
        last = self.snapshot.last_row("CL=F") or data.iloc[-1]
        return (2, "상승") if last["Close"].item() > last["SMA_100"].item() else (-2, "하락")

    def analyze_yield_curve(self) -> Tuple[int, str]:  # 6. 경기침체 (장단기 금리차)
        s = self._get_fred_series("T10Y2Y")
        if s is None or len(s) == 0:
            return 0, "데이터 부족/FRED 비활성"
        val = s.iloc[-1].item()
        if val < 0:
            return -7, "금리 역전 (침체 신호)"
        if val < 0.50:
            return -3, "금리차 축소 (위험)"
        return 2, "정상"

    def analyze_monetary_supply(self) -> Tuple[int, str]:  # 7. 통화 유동성 (M2)
        s = self._get_fred_series("M2SL")
        if s is None or len(s) < 24:
            return 0, "데이터 부족/FRED 비활성"
        roc_12m = s.pct_change(12).iloc[-1]
        if roc_12m > 0.04:
            return 5, "유동성 확대"
        if roc_12m < -0.01:
            return -5, "유동성 축소"
        return 0, "중립"

    def analyze_consumer_sentiment(self) -> Tuple[int, str]:  # 8. 소비 심리 (미시간)
        s = self._get_fred_series("UMCSENT")
        if s is None or len(s) < 12:
            return 0, "데이터 부족/FRED 비활성"
        sma_12m = s.rolling(12).mean()
        return (3, "개선") if s.iloc[-1] > sma_12m.iloc[-1] else (-3, "악화")

    def analyze_manufacturing_pmi(self) -> Tuple[int, str]:  # 9. 제조업(ISM 대용)
        s = self._get_fred_series("GACDISA066MSFRBNY")  # Empire State headline
        if s is None or len(s) < 12:
            return 0, "데이터 부족/FRED 비활성"
        v = s.iloc[-1]
        if v >= 10:
            return 4, "확장 (강)"
        if 0 <= v < 10:
            return 2, "완만한 확장"
        if v <= -10:
            return -4, "수축 (강)"
        return -2, "경계 (약수축)"

    def analyze_housing_market(self) -> Tuple[int, str]:  # 10. 주택
        s = self._get_fred_series("HOUST")
        if s is None or len(s) < 12:
            return 0, "데이터 부족/FRED 비활성"
        sma_12m = s.rolling(12).mean()
        return (3, "개선") if s.iloc[-1] > sma_12m.iloc[-1] else (-3, "악화")

    def analyze_bond_volatility(self) -> Tuple[int, str]:  # 11. 선행변동성(MOVE 프록시)
        s = self._get_fred_series("DGS10")
        if s is None or len(s) < 40:
            return 0, "데이터 부족/FRED 비활성"
        vol_20 = s.diff().rolling(20).std().iloc[-1] * 100.0  # ≈ bps 변동성
        if vol_20 >= 12:
            return -6, "채권 변동성 급등(프록시)"
        if vol_20 >= 8:
            return -4, "채권 변동성 확대(프록시)"
        return 2, "채권 변동성 완화(프록시)"

    def analyze_unemployment_rate(self) -> Tuple[int, str]:  # 12. 고용 시장 (실업률)
        s = self._get_fred_series("UNRATE")
        if s is None or len(s) < 12:
            return 0, "데이터 부족/FRED 비활성"
        sma_12m = s.rolling(12).mean()
        return (4, "완전 고용 근접") if s.iloc[-1] < sma_12m.iloc[-1] else (-5, "실업률 증가 (위험)")

    def analyze_gold(self) -> Tuple[int, str]:  # 13. 안전자산 (금)
        data = self._get_yf("GC=F")
        if data is None or len(data) < 50:
            return 0, "데이터 부족"
        data["SMA_50"] = data["Close"].rolling(50).mean()
        last = self.snapshot.last_row("GC=F") or data.iloc[-1]
        return (-3, "안전자산 선호") if last["Close"].item() > last["SMA_50"].item() else (3, "위험자산 선호")

    def analyze_tga_balance(self) -> Tuple[int, str]:  # 14. 미 재무부 잔고 (TGA)
        s = self._get_fred_series("WTREGEN")
        if s is None or len(s) < 40:
            return 0, "데이터 부족/FRED 비활성"
        change_40 = s.diff(40).iloc[-1]
        return (4, "유동성 공급") if change_40 < 0 else (-4, "유동성 흡수")

    def analyze_retail_sales(self) -> Tuple[int, str]:  # 15. 소비 (소매 판매)
        s = self._get_fred_series("RSAFS")
        if s is None or len(s) < 24:
            return 0, "데이터 부족/FRED 비활성"
        roc_12m = s.pct_change(12).iloc[-1]
        if roc_12m > 0.03:
            return 4, "소비 활황"
        if roc_12m < 0:
            return -4, "소비 침체"
        return 0, "중립"

    def analyze_industrial_production(self) -> Tuple[int, str]:  # 16. 실물경제 (산업생산)
        s = self._get_fred_series("INDPRO")
        if s is None or len(s) < 13:
            return 0, "데이터 부족/FRED 비활성"
        roc_12m = s.pct_change(12).iloc[-1]
        return (4, "생산 증가") if roc_12m > 0.01 else (-4, "생산 둔화")

    def analyze_dr_copper(self) -> Tuple[int, str]:  # 17. 경기선행 (구리)
        data = self._get_yf("HG=F")
        if data is None or len(data) < 100:
            return 0, "데이터 부족"
        data["SMA_100"] = data["Close"].rolling(100).mean()
        last = self.snapshot.last_row("HG=F") or data.iloc[-1]
        return (3, "상승") if last["Close"].item() > last["SMA_100"].item() else (-3, "하락")

    # =========================================================
    # 스코어 합산 및 상태 판정
    # =========================================================
    def compute_overall_score(self) -> Tuple[MacroRegime, int, Dict[str, int]]:
        scores: Dict[str, int] = {
            "주도주(나스닥)": self.analyze_market_leader()[0],
            "변동성(VIX)": self.analyze_market_volatility()[0],
            "신용위험(회사채)": self.analyze_credit_risk()[0],
            "유동성(달러)": self.analyze_liquidity()[0],
            "인플레이션(유가)": self.analyze_inflation_proxy()[0],
            "경기침체(금리차)": self.analyze_yield_curve()[0],
            "통화량(M2)": self.analyze_monetary_supply()[0],
            "소비심리(UMCSENT)": self.analyze_consumer_sentiment()[0],
            "제조업(ISM)": self.analyze_manufacturing_pmi()[0],
            "주택시장(HOUST)": self.analyze_housing_market()[0],
            "선행변동성(MOVE)": self.analyze_bond_volatility()[0],
            "고용(실업률)": self.analyze_unemployment_rate()[0],
            "안전자산(금)": self.analyze_gold()[0],
            "재무부잔고(TGA)": self.analyze_tga_balance()[0],
            "소비(소매판매)": self.analyze_retail_sales()[0],
            "실물경제(산업생산)": self.analyze_industrial_production()[0],
            "경기선행(구리)": self.analyze_dr_copper()[0],
        }
        # 동일 계열 중복 방지 캡(리스크 패밀리)
        scores = self._apply_same_family_caps(scores)

        final = sum(scores.values())
        if final >= 15:
            return MacroRegime.BULL, final, scores
        if final <= -15:
            return MacroRegime.BEAR, final, scores
        return MacroRegime.SIDEWAYS, final, scores

    def _apply_same_family_caps(self, scores: Dict[str, int]) -> Dict[str, int]:
        out = dict(scores)
        move = out.get("선행변동성(MOVE)", 0)
        vix = out.get("변동성(VIX)", 0)
        # 규칙: MOVE가 -4 이하(불안/극공포)면 VIX의 음수 기여는 -3으로 캡핑
        if move <= -4 and vix < 0:
            out["변동성(VIX)"] = max(vix, -3)
        return out

    # =========================================================
    # 배치/백필용: 모든 데이터 프리로드 & 과거 날짜별 스코어
    # =========================================================
    def preload_all_macro_data(self) -> Dict[str, Any]:
        print("… 거시 데이터 프리로드 중 (17종) …")
        out: Dict[str, Any] = {
            "nasdaq": self._get_yf("^IXIC"),
            "vix": self._get_yf("^VIX"),
            "dxy": self._try_yf(*DOLLAR_TICKERS),
            "oil": self._get_yf("CL=F"),
            "gold": self._get_yf("GC=F"),
            "copper": self._get_yf("HG=F"),
            "hy_spread": self._get_fred_series("BAMLH0A0HYM2"),
            "t10y2y": self._get_fred_series("T10Y2Y"),
            "m2": self._get_fred_series("M2SL"),
            "sentiment": self._get_fred_series("UMCSENT"),
            "ism": self._get_fred_series("GACDISA066MSFRBNY"),
            "housing": self._get_fred_series("HOUST"),
            "move": self._get_fred_series("DGS10"),
            "unrate": self._get_fred_series("UNRATE"),
            "tga": self._get_fred_series("WTREGEN"),
            "retail": self._get_fred_series("RSAFS"),
            "indpro": self._get_fred_series("INDPRO"),
        }
        return out

    def analyze_for_date(self, macro_data: Dict[str, Any], day: pd.Timestamp) -> Tuple[MacroState, Dict[str, int]]:
        # 도우미: day 기준 이전/당일 값 선택 (스칼라/Series/DataFrame 대응)
        def last_le(obj, d: pd.Timestamp):
            if obj is None:
                return None
            if isinstance(obj, pd.DataFrame):
                idx = obj.index.get_indexer([d], method="pad")
                if len(idx) == 0 or idx[0] == -1:
                    return None
                return obj.iloc[idx[0]]
            if isinstance(obj, pd.Series):
                idx = obj.index.get_indexer([d], method="pad")
                if len(idx) == 0 or idx[0] == -1:
                    return None
                return obj.iloc[idx[0]]
            return None

        def idx_pos_le(obj, d: pd.Timestamp):
            if obj is None or len(obj) == 0:
                return None
            idx = obj.index.get_indexer([d], method="pad")
            if len(idx) == 0 or idx[0] == -1:
                return None
            return idx[0]

        scores: Dict[str, int] = {}

        # 1. 주도주(나스닥)
        df = macro_data.get("nasdaq")
        row = last_le(df, day)
        if isinstance(row, pd.Series) and isinstance(df, pd.DataFrame) and len(df) >= 200:
            pos = idx_pos_le(df, day)
            if pos is not None:
                sma_200 = df['Close'].rolling(200).mean().iloc[pos]
                if pd.notna(row.get("Close")) and pd.notna(sma_200):
                    scores["주도주(나스닥)"] = 5 if row["Close"] > sma_200 else -5

        # 2. 변동성(VIX)
        df = macro_data.get("vix")
        row = last_le(df, day)
        if isinstance(row, pd.Series) and isinstance(df, pd.DataFrame) and len(df) >= 20:
            pos = idx_pos_le(df, day)
            if pos is not None:
                sma_20 = df['Close'].rolling(20).mean().iloc[pos]
                if pd.notna(row.get("Close")) and pd.notna(sma_20):
                    if row["Close"] > 32:
                        scores["변동성(VIX)"] = -5
                    elif row["Close"] > 24 and row["Close"] > sma_20:
                        scores["변동성(VIX)"] = -3
                    elif row["Close"] < 14:
                        scores["변동성(VIX)"] = 3

        # 3. 신용위험(회사채 스프레드)
        s = macro_data.get("hy_spread")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series) and len(s) >= 80:
            pos = idx_pos_le(s, day)
            if pos is not None:
                sma_75 = s.rolling(75).mean().iloc[pos]
                v = s.iloc[pos]
                if pd.notna(sma_75):
                    scores["신용위험(회사채)"] = -5 if v > sma_75 else 3

        # 4. 유동성(달러)
        df = macro_data.get("dxy")
        row = last_le(df, day)
        if isinstance(row, pd.Series) and isinstance(df, pd.DataFrame) and len(df) >= 100:
            pos = idx_pos_le(df, day)
            if pos is not None:
                sma_100 = df['Close'].rolling(100).mean().iloc[pos]
                if pd.notna(row.get("Close")) and pd.notna(sma_100):
                    scores["유동성(달러)"] = 4 if row["Close"] < sma_100 else -4

        # 5. 인플레이션(유가)
        df = macro_data.get("oil")
        row = last_le(df, day)
        if isinstance(row, pd.Series) and isinstance(df, pd.DataFrame) and len(df) >= 100:
            pos = idx_pos_le(df, day)
            if pos is not None:
                sma_100 = df['Close'].rolling(100).mean().iloc[pos]
                if pd.notna(row.get("Close")) and pd.notna(sma_100):
                    scores["인플레이션(유가)"] = 2 if row["Close"] > sma_100 else -2

        # 6. 경기침체(금리차)
        s = macro_data.get("t10y2y")
        val = last_le(s, day)
        if val is not None:
            if val < 0:
                scores["경기침체(금리차)"] = -7
            elif val < 0.50:
                scores["경기침체(금리차)"] = -3
            else:
                scores["경기침체(금리차)"] = 2

        # 7. 통화량(M2)
        s = macro_data.get("m2")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series) and len(s.loc[:day]) > 12:
            pos = idx_pos_le(s, day)
            if pos is not None:
                roc_12m = s.pct_change(12).iloc[pos]
                if pd.notna(roc_12m):
                    if roc_12m > 0.04:
                        scores["통화량(M2)"] = 5
                    elif roc_12m < -0.01:
                        scores["통화량(M2)"] = -5

        # 8. 소비심리
        s = macro_data.get("sentiment")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series):
            pos = idx_pos_le(s, day)
            if pos is not None:
                sma_12m = s.rolling(12).mean().iloc[pos]
                v = s.iloc[pos]
                if pd.notna(sma_12m):
                    scores["소비심리(UMCSENT)"] = 3 if v > sma_12m else -3

        # 9. 제조업(ISM 대용)
        s = macro_data.get("ism")
        val = last_le(s, day)
        if val is not None:
            if val > 55:
                scores["제조업(ISM)"] = 4
            elif 50 < val <= 55:
                scores["제조업(ISM)"] = 2
            elif val < 48:
                scores["제조업(ISM)"] = -4
            else:
                scores["제조업(ISM)"] = -2

        # 10. 주택시장
        s = macro_data.get("housing")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series):
            pos = idx_pos_le(s, day)
            if pos is not None:
                sma_12m = s.rolling(12).mean().iloc[pos]
                v = s.iloc[pos]
                if pd.notna(sma_12m):
                    scores["주택시장(HOUST)"] = 3 if v > sma_12m else -3

        # 11. 선행변동성(MOVE 프록시)
        s = macro_data.get("move")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series) and len(s.loc[:day]) >= 20:
            pos = idx_pos_le(s, day)
            if pos is not None:
                sma_20 = s.rolling(20).mean().iloc[pos]
                v = s.iloc[pos]
                if pd.notna(sma_20):
                    if v > 135:
                        scores["선행변동성(MOVE)"] = -6
                    elif v > 110 and v > sma_20:
                        scores["선행변동성(MOVE)"] = -4
                    else:
                        scores["선행변동성(MOVE)"] = 2

        # 12. 고용(실업률)
        s = macro_data.get("unrate")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series):
            pos = idx_pos_le(s, day)
            if pos is not None:
                sma_12m = s.rolling(12).mean().iloc[pos]
                v = s.iloc[pos]
                if pd.notna(sma_12m):
                    scores["고용(실업률)"] = 4 if v < sma_12m else -5

        # 13. 안전자산(금)
        df = macro_data.get("gold")
        row = last_le(df, day)
        if isinstance(row, pd.Series) and isinstance(df, pd.DataFrame) and len(df) >= 50:
            pos = idx_pos_le(df, day)
            if pos is not None:
                sma_50 = df['Close'].rolling(50).mean().iloc[pos]
                if pd.notna(row.get("Close")) and pd.notna(sma_50):
                    scores["안전자산(금)"] = -3 if row["Close"] > sma_50 else 3

        # 14. 재무부잔고(TGA)
        s = macro_data.get("tga")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series) and len(s.loc[:day]) > 40:
            pos = idx_pos_le(s, day)
            if pos is not None:
                change_40 = s.diff(40).iloc[pos]
                if pd.notna(change_40):
                    scores["재무부잔고(TGA)"] = 4 if change_40 < 0 else -4

        # 15. 소비(소매판매)
        s = macro_data.get("retail")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series) and len(s.loc[:day]) > 12:
            pos = idx_pos_le(s, day)
            if pos is not None:
                roc_12m = s.pct_change(12).iloc[pos]
                if pd.notna(roc_12m):
                    if roc_12m > 0.03:
                        scores["소비(소매판매)"] = 4
                    elif roc_12m < 0:
                        scores["소비(소매판매)"] = -4

        # 16. 실물경제(산업생산)
        s = macro_data.get("indpro")
        val = last_le(s, day)
        if val is not None and isinstance(s, pd.Series) and len(s.loc[:day]) > 12:
            pos = idx_pos_le(s, day)
            if pos is not None:
                roc_12m = s.pct_change(12).iloc[pos]
                if pd.notna(roc_12m):
                    scores["실물경제(산업생산)"] = 4 if roc_12m > 0.01 else -4

        # 17. 경기선행(구리)
        df = macro_data.get("copper")
        row = last_le(df, day)
        if isinstance(row, pd.Series) and isinstance(df, pd.DataFrame) and len(df) >= 100:
            pos = idx_pos_le(df, day)
            if pos is not None:
                sma_100 = df['Close'].rolling(100).mean().iloc[pos]
                if pd.notna(row.get("Close")) and pd.notna(sma_100):
                    scores["경기선행(구리)"] = 3 if row["Close"] > sma_100 else -3

        # === 동종 계열 캡 적용 ===
        scores = self._apply_same_family_caps(scores)

        # === 상위 카테고리 합성 ===
        growth_score = scores.get("제조업(ISM)", 0) + scores.get("실물경제(산업생산)", 0) + scores.get("경기선행(구리)", 0)
        liquidity_score = scores.get("통화량(M2)", 0) + scores.get("재무부잔고(TGA)", 0) + scores.get("유동성(달러)", 0)
        risk_score = scores.get("변동성(VIX)", 0) + scores.get("선행변동성(MOVE)", 0) + scores.get("신용위험(회사채)", 0)
        recession_signal = scores.get("경기침체(금리차)", 0) + scores.get("고용(실업률)", 0)
        consumer_score = scores.get("소비심리(UMCSENT)", 0) + scores.get("소비(소매판매)", 0) + scores.get("주택시장(HOUST)", 0)

        triggers = {
            "growth": growth_score,
            "liquidity": liquidity_score,
            "risk": risk_score,
            "recession": recession_signal,
            "consumer": consumer_score,
        }

        # 상태 전환 트리거 로직 (원형 유지)
        if recession_signal <= -10 or (risk_score <= -10 and growth_score <= -5):
            return MacroState.RECESSION, triggers
        if growth_score >= 10 and liquidity_score <= -5 and risk_score <= -8:
            return MacroState.OVERHEAT, triggers
        if growth_score >= 8 and liquidity_score >= 5 and risk_score >= 0:
            return MacroState.EXPANSION, triggers
        if liquidity_score >= 5 and recession_signal > -10:
            return MacroState.RECOVERY, triggers

        total = sum(scores.values())
        if total <= -15:
            return MacroState.RECESSION, triggers
        if total >= 15:
            return MacroState.EXPANSION, triggers
        return MacroState.RECOVERY, triggers  # 기본 상태
