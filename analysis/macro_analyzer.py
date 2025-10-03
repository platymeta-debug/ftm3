# analysis/macro_analyzer.py (전문가 수준으로 업그레이드)

import pandas as pd
import yfinance as yf
from fredapi import Fred
from enum import Enum
from datetime import datetime, timedelta
from core.config_manager import config # config 임포트

class MacroRegime(Enum):
    BULL = "강세 국면 (Risk-On)"
    BEAR = "약세 국면 (Risk-Off)"
    SIDEWAYS = "중립/혼돈 국면"

class MacroAnalyzer:
    """
    다양한 거시 경제 지표와 그 상관관계를 분석하여 시장의 체질을
    전문가 수준으로 진단하고, 위험 자산에 대한 투자 적합도를 점수화합니다.
    """
    def __init__(self):
        self.cache = {}
        self.cache_expiry = timedelta(hours=4)
        self.fred = None
        if config.fred_api_key:
            try:
                self.fred = Fred(api_key=config.fred_api_key)
                print("✅ FRED API 클라이언트가 성공적으로 초기화되었습니다.")
            except Exception as e:
                print(f"🚨 FRED API 키 초기화 실패: {e}. 일부 데이터 조회가 제한됩니다.")
        else:
            print("⚠️ FRED_API_KEY가 .env 파일에 설정되지 않았습니다. 일부 데이터 조회가 제한됩니다.")
        print("📈 거시 경제 분석기(v2.0 Expert)가 초기화되었습니다.")


    def _get_data(self, ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame | None:
        """yfinance를 통해 데이터를 가져오고 캐시합니다."""
        now = datetime.now()
        cache_key = f"{ticker}_{period}_{interval}"
        if cache_key in self.cache and (now - self.cache[cache_key]['timestamp'] < self.cache_expiry):
            return self.cache[cache_key]['data']
        try:
            data = yf.download(ticker, period=period, interval=interval, progress=False)
            if data.empty: return None
            self.cache[cache_key] = {'timestamp': now, 'data': data}
            return data
        except Exception as e:
            print(f"🚨 yfinance 데이터 다운로드 실패 ({ticker}): {e}")
            return None

    def _get_fred_data(self, series_id: str) -> pd.DataFrame | None:
        """FRED를 통해 경제 데이터를 가져오고 캐시합니다."""
        if self.fred is None: return None
        now = datetime.now()
        if series_id in self.cache and (now - self.cache[series_id]['timestamp'] < self.cache_expiry):
            return self.cache[series_id]['data']
        try:
            start_date = (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')
            data = self.fred.get_series(series_id, start_date=start_date)
            if data.empty: return None
            self.cache[series_id] = {'timestamp': now, 'data': data}
            return data
        except Exception as e:
            print(f"🚨 FRED 데이터 조회 실패 ({series_id}): {e}")
            return None

    # --- 개별 지표 분석 함수들 ---
    def analyze_market_leader(self) -> (int, str):
        """[주도주] 나스닥 지수(^IXIC)와 200일 이평선을 비교합니다."""
        data = self._get_data("^IXIC")
        if data is None or len(data) < 200: return 0, "데이터 부족"
        data['SMA_200'] = data['Close'].rolling(window=200).mean()
        last = data.iloc[-1]
        if last['Close'] > last['SMA_200']: return 5, "강세"
        return -5, "약세"

    def analyze_market_volatility(self) -> (int, str):
        """[변동성] VIX 지수(^VIX)의 절대 레벨과 추세를 분석합니다."""
        data = self._get_data("^VIX")
        if data is None or len(data) < 20: return 0, "데이터 부족"
        data['SMA_20'] = data['Close'].rolling(window=20).mean()
        last = data.iloc[-1]
        if last['Close'] > 30: return -5, "극심한 공포" # 시장 패닉
        if last['Close'] > 20 and last['Close'] > last['SMA_20']: return -3, "공포 확산" # 위험 회피
        if last['Close'] < 15: return 3, "시장 안정" # 위험 선호
        return 0, "중립"

    def analyze_credit_risk(self) -> (int, str):
        """[신용위험] 미국 하이일드 채권 스프레드(BAMLH0A0HYM2)를 분석합니다."""
        data = self._get_fred_data("BAMLH0A0HYM2")
        if data is None or len(data) < 50: return 0, "데이터 부족"
        data_sma50 = data.rolling(50).mean()
        # 스프레드가 확대(위험 증가)되면 암호화폐 시장에 악재
        if data.iloc[-1] > data_sma50.iloc[-1]: return -5, "신용 경색"
        return 3, "자금 원활"

    def analyze_liquidity(self) -> (int, str):
        """[유동성] 달러 인덱스(DX-Y.NYB) 추세를 분석합니다."""
        data = self._get_data("DX-Y.NYB")
        if data is None or len(data) < 50: return 0, "데이터 부족"
        data['SMA_50'] = data['Close'].rolling(window=50).mean()
        # 달러 약세는 위험자산 선호 심리 강화
        if data.iloc[-1]['Close'] < data.iloc[-1]['SMA_50']: return 4, "달러 약세"
        return -4, "달러 강세"

    def analyze_inflation_proxy(self) -> (int, str):
        """[인플레이션] 국제 유가(CL=F) 추세를 분석합니다."""
        data = self._get_data("CL=F")
        if data is None or len(data) < 50: return 0, "데이터 부족"
        data['SMA_50'] = data['Close'].rolling(window=50).mean()
        # 유가 상승은 인플레이션 헤지 자산(BTC)에 긍정적일 수 있음
        if data.iloc[-1]['Close'] > data.iloc[-1]['SMA_50']: return 2, "상승"
        return -2, "하락"

    # --- 종합 진단 로직 ---
    def diagnose_macro_regime(self) -> tuple[MacroRegime, int, dict]:
        """
        모든 거시 지표를 종합하고, 상관관계를 고려하여 최종 시장 체제를 진단합니다.
        :return: (시장 체제 Enum, 최종 점수, 상세 점수 딕셔셔너리)
        """
        scores = {
            "주도주(나스닥)": self.analyze_market_leader()[0],
            "변동성(VIX)": self.analyze_market_volatility()[0],
            "신용위험(회사채)": self.analyze_credit_risk()[0],
            "유동성(달러)": self.analyze_liquidity()[0],
            "인플레이션(유가)": self.analyze_inflation_proxy()[0],
        }
        base_score = sum(scores.values())
        final_score = base_score

        # === 상관관계 분석 및 점수 조정 (전문가 로직) ===
        # 1. 'Flight to Safety' 시나리오: 주도주 약세 + 신용위험 증가는 매우 강력한 약세 신호
        if scores["주도주(나스닥)"] < 0 and scores["신용위험(회사채)"] < 0:
            final_score -= 5 # 패널티 강화
            scores["상관관계 조정"] = -5

        # 2. 'Risk-On' 시나리오: 주도주 강세 + 변동성 안정 + 달러 약세는 매우 강력한 강세 신호
        if scores["주도주(나스닥)"] > 0 and scores["변동성(VIX)"] > 0 and scores["유동성(달러)"] > 0:
            final_score += 5 # 보너스 강화
            scores["상관관계 조정"] = 5

        print(f"📊 거시 경제 진단: 기본점수={base_score}, 최종점수={final_score} | 상세: {scores}")

        if final_score >= 7:
            return MacroRegime.BULL, final_score, scores
        elif final_score <= -7:
            return MacroRegime.BEAR, final_score, scores
        else:
            return MacroRegime.SIDEWAYS, final_score, scores