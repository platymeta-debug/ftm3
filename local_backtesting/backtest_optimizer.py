# -*- coding: utf-8 -*-
# local_backtesting/backtest_optimizer.py
"""
V7 — 2018년부터 MacroAnalyzer 레짐 에피소드별 최적화
- 2018-01-01부터 4h 전구간 수집
- MacroAnalyzer 기준으로 BULL/BEAR 연속 구간(period) 도출
- 에피소드별로 최적화/재평가/리포트 저장
- Bayes 최소화 부호 보정, 동적 min_trades
- backtesting size 규칙 강제(sanitize)
"""

import multiprocessing
from backtesting import backtesting
backtesting.Pool = multiprocessing.Pool

import pandas as pd
import numpy as np
import json
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
from collections import deque
import sys
import os
import math
from tqdm import tqdm
from datetime import datetime
# 한글 라벨
SYMBOL_NAME = {"BTCUSDT": "비트코인", "ETHUSDT": "이더리움"}
REGIME_NAME = {"BULL": "강세장(불장)", "BEAR": "약세장(하락장)", "SIDEWAYS": "횡보장"}

# --- 경로 설정 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# .env 로드(있으면)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(project_root, ".env"))
except Exception:
    pass

from analysis import indicator_calculator, data_fetcher
from analysis.confluence_engine import ConfluenceEngine
from analysis.macro_analyzer import MacroAnalyzer, MacroRegime
from analysis.risk_sizing import calc_order_qty
from core.config_manager import ConfigManager
#from core.config_manager import config

# (선택형 최적화기)
try:
    from local_backtesting.optimizers import run_ga, run_bayes
    _HAS_OPTIMIZERS = True
except Exception:
    _HAS_OPTIMIZERS = False


# ---- 안전 폴백: 전략 설정 읽기 ----
def get_strategy_configs_safe(config, regime: str):
    """ ConfigManager 객체를 받아 안전하게 전략 설정을 읽습니다. """
    if hasattr(config, "get_strategy_configs"):
        try:
            return config.get_strategy_configs(regime)
        except Exception:
            pass
    for attr in ("strategy_configs", "strategies", "strategy", "configs"):
        try:
            store = getattr(config, attr)
            if isinstance(store, dict):
                val = store.get(regime)
                if isinstance(val, dict):
                    return val
        except Exception:
            pass
    try:
        if hasattr(config, "get"):
            store = config.get("strategies", {})
            if isinstance(store, dict):
                val = store.get(regime)
                if isinstance(val, dict):
                    return val
    except Exception:
        pass
    return {
        "TrendStrategy": {},
        "OscillatorStrategy": {},
        "ComprehensiveStrategy": {},
    }


# 덮어 쓸 함수: _to_jsonable_dict

def _to_jsonable_dict(data):
    """ [V8.2] 재귀적으로 딕셔너리와 리스트를 순회하며 JSON으로 변환 불가능한 타입을 표준 타입으로 변환합니다. """
    # 딕셔너리인 경우, 모든 값에 대해 재귀적으로 함수를 호출합니다.
    if isinstance(data, dict):
        return {k: _to_jsonable_dict(v) for k, v in data.items()}
    # 리스트인 경우, 모든 항목에 대해 재귀적으로 함수를 호출합니다.
    if isinstance(data, list):
        return [_to_jsonable_dict(i) for i in data]
    
    # Numpy의 특수 숫자 타입(int64, float64 등)을 파이썬 기본 타입으로 변환합니다.
    if hasattr(data, 'dtype'):
        if np.issubdtype(data.dtype, np.integer):
            return int(data)
        if np.issubdtype(data.dtype, np.floating):
            return float(data)
        if np.issubdtype(data.dtype, np.bool_):
            return bool(data)
    
    # Pandas의 Timestamp 타입을 문자열로 변환합니다.
    if isinstance(data, pd.Timestamp):
        return data.isoformat()
        
    # 그 외 JSON으로 변환 가능한 타입은 그대로 반환합니다.
    return data


# === 신규: 2020-01-01부터 일봉 전구간 수집 ===
def fetch_klines_since_2020(binance_client: Client, symbol: str, interval: str = "1d") -> pd.DataFrame:
    """
    Binance SDK의 get_historical_klines로 2020-01-01 UTC부터 전구간 수집.
    """
    start_str = "1 Jan, 2020" # 시작 시점 변경
    raw = binance_client.get_historical_klines(symbol, interval, start_str)
    if not raw:
        return pd.DataFrame()

    cols = ["Open time","Open","High","Low","Close","Volume","Close time",
            "Quote asset volume","Number of trades","Taker buy base asset volume",
            "Taker buy quote asset volume","Ignore"]
    df = pd.DataFrame(raw, columns=cols)
    df["Open time"] = pd.to_datetime(df["Open time"], unit="ms", utc=True)
    df.set_index("Open time", inplace=True)
    for c in ["Open","High","Low","Close","Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[["Open","High","Low","Close","Volume"]].dropna()
    # 컬럼 대문자 정규화 (이미 대문자이지만 일관성 유지)
    df.columns = [c.capitalize() for c in df.columns]
    return df


# === 신규: MacroAnalyzer 레짐을 연속 구간으로 압축 ===
# === 신규: MacroAnalyzer 레짐을 연속 구간으로 압축 ===
def collapse_regimes_to_periods(df: pd.DataFrame, macro_data: dict, config: ConfigManager) -> dict:
    """
    df(index: DatetimeIndex, cols: Open/High/Low/Close/Volume)에 대해
    MacroAnalyzer로 시점별 레짐을 계산하고 동일 레짐 연속 구간을 (start,end)로 압축.
    너무 짧은 구간 제거.
    """
    # ▼▼▼ [수정] UTC 시간대 정보를 '날짜' 기준으로 변환하여 비교 기준을 통일합니다. ▼▼▼
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    # ▲▲▲ [수정] ▲▲▲

    ma = MacroAnalyzer(config)
    regimes = []

    # ▼▼▼ [오류 수정] 변경된 analyze_for_date 함수 호출 및 결과 매핑 ▼▼▼
    for ts in tqdm(df.index, desc="      레짐 레이블링"):
        # 1. 새로운 함수 analyze_for_date 호출
        state, _ = ma.analyze_for_date(macro_data, ts)

        # 2. 새로운 상태(MacroState)를 기존 국면(MacroRegime)으로 매핑
        if state in [MacroState.EXPANSION]:
            regime = MacroRegime.BULL
        elif state in [MacroState.RECESSION]:
            regime = MacroRegime.BEAR
        else: # RECOVERY, OVERHEAT
            regime = MacroRegime.SIDEWAYS
        regimes.append(regime.name)
    # ▲▲▲ [오류 수정] 종료 ▲▲▲

    ser = pd.Series(regimes, index=df.index, name="Regime")

    periods = {"BULL": [], "BEAR": [], "SIDEWAYS": []}
    if ser.empty:
        return periods

    prev = ser.iloc[0]
    run_start = ser.index[0]
    for i in range(1, len(ser)):
        t = ser.index[i]
        r = ser.iloc[i]
        if r != prev:
            periods.setdefault(prev, []).append({"start": run_start, "end": ser.index[i-1]})
            run_start = t
            prev = r
    periods.setdefault(prev, []).append({"start": run_start, "end": ser.index[-1]})

    MIN_BARS = 50 # 일봉 기준 약 50일
    cleaned = {"BULL": [], "BEAR": [], "SIDEWAYS": []}
    for k, lst in periods.items():
        for p in lst:
            mask = (df.index >= p["start"]) & (df.index <= p["end"])
            if mask.sum() >= MIN_BARS:
                cleaned[k].append(p)
    return cleaned


def segment_data_by_regime(klines_df: pd.DataFrame, macro_data: dict) -> dict:
    """
    (참고용) 전체 시계열에 대해 일자별 레짐을 라벨링한 뒤 단순 필터링으로 분할.
    - 이번 버전에서는 collapse_regimes_to_periods로 에피소드별 운용이 기본이지만,
      폴백/디버깅용으로 남겨둠.
    """
    print("\n...과거 데이터 전체에 대한 거시 경제 분석을 시작합니다...")
    ma = MacroAnalyzer()
    regimes = []
    for dt in tqdm(klines_df.index, desc="과거 시장 상황 분석 중"):
        regime, _, _ = ma.diagnose_macro_regime_for_date(dt, macro_data)
        regimes.append(regime.name if isinstance(regime, MacroRegime) else str(regime))
    klines_df = klines_df.copy()
    klines_df['Regime'] = regimes
    out = {
        "BULL": klines_df[klines_df['Regime'] == 'BULL'],
        "BEAR": klines_df[klines_df['Regime'] == 'BEAR'],
        "SIDEWAYS": klines_df[klines_df['Regime'] == 'SIDEWAYS']
    }
    print("...거시 경제 분석 및 데이터 구간 선별 완료!")
    print(f"   - 강세장(BULL) 데이터: {len(out['BULL'])}개 캔들")
    print(f"   - 약세장(BEAR) 데이터: {len(out['BEAR'])}개 캔들")
    print(f"   - 횡보장(SIDEWAYS) 데이터: {len(out['SIDEWAYS'])}개 캔들")
    return out


class OptoRunner(Strategy):
    """
    분석(ConfluenceEngine) + 실행정책 시뮬(부분익절/타임스탑/트레일링)
    + 리스크 기반 포지션 사이징(상대 크기)
    """

    # 실행정책
    open_threshold = 12.0
    risk_reward_ratio = 2.0
    sl_atr_multiplier = 1.5
    trend_entry_confirm_count = 3
    risk_per_trade = 0.01
    
    # TrendStrategy
    ema_short = 20
    ema_long = 50
    score_strong_trend = 5 # <-- 누락되었던 파라미터

    # OscillatorStrategy
    rsi_period = 14
    rsi_oversold = 30
    score_oversold = 5 # <-- 누락되었던 파라미터

    # ComprehensiveStrategy
    score_macd_cross_up = 2 # <-- 누락되었던 파라미터
    adx_threshold = 25
    score_adx_strong = 3 # <-- 누락되었던 파라미터
    
    # VolatilityStrategy
    bb_len = 20
    bb_std = 2.0
    kc_len = 20
    kc_atr_mult = 2.0
    score_squeeze_breakout_up = 5

    # CounterTrendStrategy
    williams_period = 14
    williams_overbought = -20
    williams_oversold = -80
    score_counter_buy = 4

    # MomentumStrategy
    roc_threshold = 0.5
    score_momentum_buy = 3

    # OrderBlockStrategy
    lookback_period = 10
    volume_factor = 1.5
    score_bullish_ob = 6

    # (최적화 대상이 아닌 고정값들)
    exec_partial = "1.0"
    exec_time_stop_bars = 0
    exec_trailing_mode = "off"
    exec_trailing_k = 0.0
    max_exposure_frac = 0.30
    symbol = "BTCUSDT"
    market_regime = "BULL"

    # ====== 상태 변수 ======
    _recent_scores: deque
    _in_pos: bool
    _side: str
    _entry_px: float
    _entry_atr: float
    _sl_px: float
    _tp_plan: list
    _bars_held: int

    def init(self):
        # --- ▼▼▼ [최종 수정] 동적 계산 로직 적용 ▼▼▼ ---
        # 분석 엔진 초기화
        strategy_configs = {
            "TrendStrategy": {
                "enabled": True, "ema_short": int(self.ema_short), "ema_long": int(self.ema_long), 
                "score_strong_trend": int(self.score_strong_trend)
            },
            "OscillatorStrategy": {
                "enabled": True, "rsi_period": int(self.rsi_period), "rsi_oversold": int(self.rsi_oversold), 
                "score_oversold": int(self.score_oversold)
            },
            "ComprehensiveStrategy": {
                "enabled": True, "adx_threshold": int(self.adx_threshold), 
                "score_macd_cross_up": int(self.score_macd_cross_up), "score_adx_strong": int(self.score_adx_strong)
            },
            "VolatilityStrategy": {
                "enabled": True, "bb_len": int(self.bb_len), "bb_std": float(self.bb_std), 
                "kc_len": int(self.kc_len), "kc_atr_mult": float(self.kc_atr_mult), 
                "score_squeeze_breakout_up": int(self.score_squeeze_breakout_up), 
                "score_squeeze_breakout_down": -int(self.score_squeeze_breakout_up)
            },
            "CounterTrendStrategy": {
                "enabled": True, "williams_period": int(self.williams_period), 
                "williams_overbought": int(self.williams_overbought), "williams_oversold": int(self.williams_oversold), 
                "score_counter_buy": int(self.score_counter_buy), "score_counter_sell": -int(self.score_counter_buy)
            },
            "MomentumStrategy": {
                "enabled": True, "roc_threshold": float(self.roc_threshold), 
                "score_momentum_buy": int(self.score_momentum_buy), "score_momentum_sell": -int(self.score_momentum_buy)
            },
            "OrderBlockStrategy": {
                "enabled": True, "lookback_period": int(self.lookback_period), 
                "volume_factor": float(self.volume_factor), "score_bullish_ob": int(self.score_bullish_ob), 
                "score_bearish_ob": -int(self.score_bullish_ob)
            }
        }
        temp_config = ConfigManager()
        self.engine = ConfluenceEngine(Client("", ""), config=temp_config, strategy_configs=strategy_configs)
        # --- ▲▲▲ [최종 수정] ---
        # 지표 캐시
        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)

        # 점수 윈도우
        self._recent_scores = deque(maxlen=int(self.trend_entry_confirm_count))

        # 실행 상태
        self._in_pos = False
        self._side = None
        self._entry_px = np.nan
        self._entry_atr = np.nan
        self._sl_px = np.nan
        self._tp_plan = []  # [{"px":float,"qty":float,"done":False}, ...]
        self._bars_held = 0

        # exec_partial 파싱(상대 비율로 사용)
        if isinstance(self.exec_partial, str):
            parts = [p.strip() for p in self.exec_partial.split(",") if p.strip()]
            self._partials = [float(x) for x in parts] if parts else [1.0]
        elif isinstance(self.exec_partial, (list, tuple)):
            self._partials = [float(x) for x in self.exec_partial]
        else:
            self._partials = [1.0]

    # ---- 내부 유틸 ----
    @staticmethod
    def _scale_tp(entry_px: float, tp_base: float, side: str, mult: float) -> float:
        if mult == 1.0:
            return tp_base
        if side == "BUY":
            r = tp_base - entry_px
            return entry_px + r * mult
        else:
            r = entry_px - tp_base
            return entry_px - r * mult

    def _reset_pos_state(self):
        self._in_pos = False
        self._side = None
        self._entry_px = np.nan
        self._entry_atr = np.nan
        self._sl_px = np.nan
        self._tp_plan = []
        self._bars_held = 0


    # === backtesting 규칙을 만족시키는 size 정규화 ===
    @staticmethod
    def _sanitize_size(qty):
        """
        backtesting Assertion:
          - 0 < size < 1  (지분 비율)
          - 또는 round(size) == size >= 1  (정수 유닛)
        위반/비정상(qty<=0, NaN, inf)은 None → 스킵.
        """
        if not isinstance(qty, (int, float, np.floating)) or not np.isfinite(qty):
            return None
        if 0 < qty < 1:
            return float(qty)
        if qty >= 1:
            return int(max(1, math.floor(qty)))
        return None

    # ---- 진입/청산 ----
    def _maybe_enter(self, side: str):
        if self._in_pos:
            return
        idx = len(self.data) - 1
        cur = self.indicators.iloc[idx]
        atr = cur.get("ATRr_14", 0) or cur.get("ATR_14", 0)
        if not atr or np.isnan(atr) or atr <= 0:
            return

        px = float(self.data.Close[-1])
        sl_d = float(atr) * float(self.sl_atr_multiplier)  # 손절 거리
        rr = float(self.risk_reward_ratio)

        if side == "BUY":
            sl = px - sl_d
            tp_base = px + sl_d * rr
        else:
            sl = px + sl_d
            tp_base = px - sl_d * rr

        # ===== 리스크 기반 '상대 크기' 계산 =====
        try:
            equity = float(self._broker.equity)
        except Exception:
            equity = 10_000.0
        margin = float(getattr(self._broker, "margin", 1/10)) or 1/10

        qty = calc_order_qty(
            price=px,
            atr=float(atr),
            sl_atr_mult=float(self.sl_atr_multiplier),
            equity=equity,
            risk_per_trade=float(self.risk_per_trade),
            max_exposure_frac=float(self.max_exposure_frac),
            margin=margin,
            min_notional=5.0,
            qty_step=1e-6,
            min_qty=1e-6,
        )
        safe_qty = self._sanitize_size(qty)
        if safe_qty is None:
            return

        if side == "BUY":
            self.buy(size=safe_qty)
        else:
            self.sell(size=safe_qty)

        # 상태 저장
        self._in_pos = True
        self._side = side
        self._entry_px = px
        self._entry_atr = float(atr)
        self._sl_px = sl
        self._bars_held = 0

        # 멀티 TP 계획
        steps = [0.5, 1.0, 1.5] if len(self._partials) == 3 else [1.0] * len(self._partials)
        self._tp_plan = []
        remain = float(qty)
        for i, (w, m) in enumerate(zip(self._partials, steps)):
            tp_px = self._scale_tp(px, tp_base, side, m)
            if i < len(self._partials) - 1:
                sub_qty = float(qty * float(w))
            else:
                sub_qty = float(remain)
            remain -= sub_qty
            self._tp_plan.append({"px": tp_px, "qty": sub_qty, "done": False})

    def _maybe_exit_by_tp(self):
        if not self._in_pos or not self._tp_plan:
            return
        last = float(self.data.Close[-1])
        for item in self._tp_plan:
            if item["done"]:
                continue
            hit = (last >= item["px"]) if self._side == "BUY" else (last <= item["px"])
            if hit:
                safe_qty = self._sanitize_size(item["qty"])
                if safe_qty is None:
                    item["done"] = True
                    continue
                if self._side == "BUY":
                    self.sell(size=safe_qty)
                else:
                    self.buy(size=safe_qty)
                item["done"] = True

        if all(x["done"] for x in self._tp_plan):
            self._reset_pos_state()

    def _maybe_exit_by_sl(self):
        if not self._in_pos:
            return
        last_low = float(self.data.Low[-1])
        last_high = float(self.data.High[-1])
        touched = (last_low <= self._sl_px) if self._side == "BUY" else (last_high >= self._sl_px)
        if touched:
            self.position.close()
            self._reset_pos_state()

    def _maybe_time_stop(self):
        if not self._in_pos:
            return
        k = int(self.exec_time_stop_bars or 0)
        if k > 0:
            self._bars_held += 1
            if self._bars_held >= k:
                self.position.close()
                self._reset_pos_state()

    def _maybe_trailing(self):
        if not self._in_pos:
            return
        mode = (self.exec_trailing_mode or "off").lower()
        if mode == "off":
            return
        last = float(self.data.Close[-1])
        if mode == "atr":
            atr = float(self._entry_atr or 0)
            k = float(self.exec_trailing_k or 0)
            if atr <= 0 or k <= 0:
                return
            trail = atr * k
        else:
            k = float(self.exec_trailing_k or 0)
            if k <= 0:
                return
            trail = last * (k / 100.0)

        if self._side == "BUY":
            new_sl = max(self._entry_px, last - trail)
            self._sl_px = max(self._sl_px, new_sl)
        else:
            new_sl = min(self._entry_px, last + trail)
            self._sl_px = min(self._sl_px, new_sl)

    # ---- 백테스트 루프 ----
    def next(self):
        # ▼▼▼ [핵심 수정] 오류를 유발하는 불필요한 코드를 모두 제거한 최종 버전입니다. ▼▼▼
        idx = len(self.data) - 1
        cur_indicators = self.indicators.iloc[:idx + 1]

        if len(cur_indicators) < int(self.trend_entry_confirm_count):
            return

        # 1. 현재 market_regime에 맞는 전략 목록을 가져옴
        strategies_for_regime = self.engine._get_strategies_for_regime(self.market_regime)
        
        # 2. 전략 목록을 인자로 전달하여 점수 계산
        current_score, _ = self.engine._calculate_tactical_score(cur_indicators, strategies_for_regime)
        
        self._recent_scores.append(current_score)
        if len(self._recent_scores) < int(self.trend_entry_confirm_count):
            return
        
        avg_score = sum(self._recent_scores) / len(self._recent_scores)
        side = None

        if self.market_regime == "BULL" and avg_score >= float(self.open_threshold):
            side = "BUY"
        elif self.market_regime == "BEAR" and avg_score <= -float(self.open_threshold):
            side = "SELL"
        elif self.market_regime == "SIDEWAYS":
            if current_score >= float(self.open_threshold):
                side = "BUY" 
            elif current_score <= -float(self.open_threshold):
                side = "SELL"

        if not self._in_pos and side:
            self._maybe_enter(side)
            
        if self._in_pos:
            self._maybe_trailing()
            self._maybe_exit_by_tp()
            self._maybe_exit_by_sl()
            self._maybe_time_stop()


# 결과 요약에 표시할 파라미터 키
BEST_PARAM_KEYS = [
    # 실행정책(임계 포함)
    "open_threshold","risk_reward_ratio","sl_atr_multiplier","trend_entry_confirm_count",
    "exec_partial","exec_time_stop_bars","exec_trailing_mode","exec_trailing_k",
    # 리스크 사이징
    "risk_per_trade","max_exposure_frac",
    # 분석 파라미터
    "ema_short","ema_long","score_strong_trend",
    "rsi_period","rsi_oversold","score_oversold",
    "score_macd_cross_up","adx_threshold","score_adx_strong",
]


# ---- 공통 유틸: 파라미터→백테스트 실행 ----
# 덮어 쓸 함수: run_backtest_with_params

METRIC_MAP = {
    "BULL": "Calmar Ratio",
    "BEAR": "Sortino Ratio",
    "SIDEWAYS": "Profit Factor"
}

def run_backtest_with_params(df_capitalized: pd.DataFrame, params: dict, initial_cash: int, symbol: str, regime: str):
    OptoRunner.symbol = symbol
    OptoRunner.market_regime = regime
    bt = FractionalBacktest(df_capitalized, OptoRunner, cash=initial_cash, commission=.002, margin=1/10, finalize_trades=True)
    stats = bt.run(**params)
    
    min_trades = max(20, len(df_capitalized) // 250)
    trades = int(stats.get("# Trades", 0) or 0)
    
    # 거래 횟수가 부족할 경우, -1e9 대신 Return을 기반으로 한 현실적인 패널티 부여
    if trades < min_trades:
        ret = float(stats.get("Return [%]", -99) or -99)
        # 0보다 크면 패널티를 덜 주고, 음수면 더 큰 패널티를 줌
        score = ret if ret > 0 else ret * 5 
        return stats, score, f"거래 부족({trades}/{min_trades}회)"

    metric_name = METRIC_MAP.get(regime, "Sharpe Ratio")
    primary_metric = float(stats.get(metric_name, 0) or 0)
    
    equity_curve = stats.get("_equity_curve", {}).get("Equity", pd.Series(dtype=float))
    stability_bonus = 0
    if not equity_curve.empty and len(equity_curve) > 1:
        x, y = np.arange(len(equity_curve)), equity_curve.values
        slope, _ = np.polyfit(x, y, 1)
        stability_bonus = (slope / initial_cash) * 1000

    final_score = primary_metric + stability_bonus
    return stats, final_score, f"{metric_name} + Stability"


def get_param_spaces():
    """확장된 탐색 공간: 신규 전략 3종의 파라미터 포함"""
    return {
        # 실행정책
        "open_threshold":       {"type":"int", "choices":[10, 12, 15, 18]},
        "risk_reward_ratio":    {"type":"float", "low":1.5, "high":3.5},
        "sl_atr_multiplier":    {"type":"float", "low":1.2, "high":3.0},
        "trend_entry_confirm_count": {"type":"int", "choices":[2, 3]},
        "risk_per_trade":       {"type":"float", "low":0.005, "high":0.02},

        # TrendStrategy
        "ema_short":            {"type":"int", "low":10, "high":30},
        "ema_long":             {"type":"int", "low":40, "high":90},
        
        # OscillatorStrategy
        "rsi_oversold":         {"type":"int", "low":20, "high":35},

        # ComprehensiveStrategy
        "adx_threshold":        {"type":"int", "low":18, "high":30},

        # VolatilityStrategy
        "bb_len":               {"type":"int", "choices":[20, 30]},
        "bb_std":               {"type":"float", "low":2.0, "high":2.5},

        # CounterTrendStrategy (신규)
        "williams_period":      {"type":"int", "choices":[14, 28]},
        "williams_overbought":  {"type":"int", "low":-25, "high":-15},
        "williams_oversold":    {"type":"int", "low":-85, "high":-75},
        "score_counter_buy":    {"type":"int", "choices":[3, 4, 5]},

        # MomentumStrategy (신규)
        "roc_threshold":        {"type":"float", "low":0.1, "high":1.0},
        "score_momentum_buy":   {"type":"int", "choices":[2, 3, 4]},
        
        # OrderBlockStrategy (신규)
        "lookback_period":      {"type":"int", "low":8, "high":20},
        "volume_factor":        {"type":"float", "low":1.5, "high":2.5},
        "score_bullish_ob":     {"type":"int", "choices":[5, 6, 7]},
    }


def grid_choice_count(param_spaces):
    total = 1
    for s in param_spaces.values():
        ch = s.get("choices")
        if ch:
            total *= len(ch)
    return total


def choose_method_auto(param_spaces):
    env = os.getenv("OPT_METHOD", "auto").lower()
    if env in ("grid", "ga", "bayes"):
        return env
    combos = grid_choice_count(param_spaces)
    if combos <= 3000:
        return "grid"
    return "bayes" if _HAS_OPTIMIZERS else "grid"


# 덮어 쓸 코드 블록: if __name__ == '__main__':

if __name__ == '__main__':
    config = ConfigManager()
    backtesting.Pool = multiprocessing.Pool

    symbols_to_optimize = ["BTCUSDT", "ETHUSDT"]
    initial_cash = 10_000

    binance_client = Client(
        getattr(config, "api_key", "") or "",
        getattr(config, "api_secret", "") or ""
    )
    
    optimal_settings_file = os.path.join(project_root, "optimal_settings.json")
    strategies_optimized_file = os.path.join(project_root, "strategies_optimized.json")
    
    all_settings = {}
    all_strategies = {}
    all_validation_results = {} # 최종 요약용 딕셔너리 추가
    
    ma = MacroAnalyzer(config)
    macro_preloaded = ma.preload_all_macro_data()
    param_spaces = get_param_spaces()
    
    # === Walk-Forward 설정 ===
    START_YEAR = 2020
    TRAIN_YEARS = 2
    VALIDATION_YEARS = 1
    
    for symbol in symbols_to_optimize:
        print(f"\n\n{'='*70}\n📦 Walk-Forward 최적화 시작: {symbol}\n{'='*70}")
        
        klines = fetch_klines_since_2020(binance_client, symbol, "1d")
        if klines is None or len(klines) < 365 * (TRAIN_YEARS + VALIDATION_YEARS):
            print(f"[SKIP] {symbol} 데이터 부족 (전체 기간)")
            continue
        
        current_year = START_YEAR
        final_best_params = {}
        all_validation_results[symbol] = []

        while current_year + TRAIN_YEARS + VALIDATION_YEARS <= datetime.now().year + 1:
            train_start = f"{current_year}-01-01"
            train_end = f"{current_year + TRAIN_YEARS - 1}-12-31"
            validation_start = f"{current_year + TRAIN_YEARS}-01-01"
            validation_end = f"{current_year + TRAIN_YEARS + VALIDATION_YEARS - 1}-12-31"
            
            print(f"\n\n--- 🔄 Period: Train[{train_start} ~ {train_end}] -> Validate[{validation_start} ~ {validation_end}] ---")

            train_df = klines.loc[train_start:train_end]
            validation_df = klines.loc[validation_start:validation_end]

            if train_df.empty or validation_df.empty:
                print("    [SKIP] 해당 기간 데이터 부족")
                current_year += VALIDATION_YEARS
                continue
            
            print(f"    - [학습 단계] 데이터 {len(train_df)}개로 최적 파라미터 탐색...")
            periods_by_regime = collapse_regimes_to_periods(train_df, macro_preloaded, config)
            
            period_best_params = {}
            for regime in ["BULL", "BEAR", "SIDEWAYS"]:
                episodes = periods_by_regime.get(regime, [])
                if not episodes: continue
                
                regime_train_df = pd.concat([train_df.loc[ep["start"]:ep["end"]] for ep in episodes]).sort_index()
                regime_train_df = regime_train_df[~regime_train_df.index.duplicated(keep='first')]
                if len(regime_train_df) < MIN_BARS: continue

                print(f"      - [{regime}] 국면 최적화 중 ({len(regime_train_df)}개 데이터)...")
                def objective(params):
                    if params.get("ema_short", 99) >= params.get("ema_long", 0): return -1e9
                    _, score, _ = run_backtest_with_params(regime_train_df, params, initial_cash, symbol, regime)
                    return score if pd.notna(score) else -1e9

                best_params, best_score = run_bayes(objective, param_spaces, n_calls=100)
                period_best_params[regime] = best_params
                print(f"        -> 최적 점수: {best_score:.3f}")

            print(f"\n    - [검증 단계] 학습된 파라미터로 미관측 데이터({len(validation_df)}개) 최종 성과 측정...")
            if not period_best_params:
                print("      -> 학습된 파라미터가 없어 검증을 건너뜁니다.")
            else:
                for regime, params in period_best_params.items():
                    # ▼▼▼ [요청사항 반영] 검증 단계에서 상세 리포트 및 최종 성과 요약 출력 ▼▼▼
                    final_bt = FractionalBacktest(validation_df, OptoRunner, cash=initial_cash, commission=.002, margin=1/10, finalize_trades=True)
                    stats_eval = final_bt.run(**params)
                    metric_name = METRIC_MAP.get(regime, "Sharpe Ratio")
                    
                    print(f"\n      --- ✅ [{symbol}/{regime}] 검증 완료! ({validation_start} ~ {validation_end}) ---")
                    print(f"         - 적용 파라미터: {json.dumps({k: v for k, v in params.items() if k in ['open_threshold', 'risk_reward_ratio', 'sl_atr_multiplier']})}")
                    print(f"         - 최종 성과 요약:")
                    print(f"           - {metric_name}: {stats_eval.get(metric_name, 'N/A'):.3f}")
                    print(f"           - Return [%]: {stats_eval.get('Return [%]', 0):.2f}%")
                    print(f"           - # Trades: {stats_eval.get('# Trades', 0)}")

                    # 최종 요약을 위해 결과 저장
                    all_validation_results[symbol].append({
                        "period": f"{validation_start}~{validation_end}",
                        "regime": regime,
                        "return_pct": stats_eval.get('Return [%]', 0),
                        "trades": stats_eval.get('# Trades', 0)
                    })

                    results_root = os.path.join(os.path.dirname(__file__), "results", symbol, f"validation_{validation_start}_{validation_end}")
                    os.makedirs(results_root, exist_ok=True)
                    html_path = os.path.join(results_root, f"{symbol}_{regime}_VALIDATION_REPORT.html")
                    try: 
                        final_bt.plot(open_browser=False, filename=html_path)
                        print(f"         - 리포트 저장 완료: {html_path}")
                    except Exception as e: 
                        print(f"         - [WARN] HTML 리포트 생성 실패: {e}")
                    # ▲▲▲ [요청사항 반영] 종료 ▲▲▲
            
            final_best_params = period_best_params
            current_year += VALIDATION_YEARS

        print(f"\n--- ✅ [{symbol}] Walk-Forward 최적화 완료! 최종 파라미터를 저장합니다. ---")
        if not final_best_params:
            print("    [WARN] 유효한 최적 파라미터를 찾지 못해 파일을 저장하지 않습니다.")
            continue

        for regime, best_params_dict in final_best_params.items():
            all_settings.setdefault(regime, {})[symbol] = {
                "OPEN_TH": int(best_params_dict.get("open_threshold")), 
                "RR_RATIO": float(best_params_dict.get("risk_reward_ratio")), 
                "SL_ATR_MULTIPLIER": float(best_params_dict.get("sl_atr_multiplier")), 
                "TREND_ENTRY_CONFIRM_COUNT": int(best_params_dict.get("trend_entry_confirm_count")), 
                "risk_per_trade": float(best_params_dict.get("risk_per_trade")),
            }
            
            all_strategies.setdefault(regime, {})
            STRATEGY_PARAM_MAP = {
                "TrendStrategy": ["ema_short", "ema_long", "score_strong_trend"], "OscillatorStrategy": ["rsi_period", "rsi_oversold", "score_oversold"],
                "ComprehensiveStrategy": ["adx_threshold", "score_macd_cross_up", "score_adx_strong"], "VolatilityStrategy": ["bb_len", "bb_std", "kc_len", "kc_atr_mult", "score_squeeze_breakout_up"],
                "CounterTrendStrategy": ["williams_period", "williams_overbought", "williams_oversold", "score_counter_buy"], "MomentumStrategy": ["roc_threshold", "score_momentum_buy"],
                "OrderBlockStrategy": ["lookback_period", "volume_factor", "score_bullish_ob"],
            }
            for strat_name, params_list in STRATEGY_PARAM_MAP.items():
                params_for_strat = {p: best_params_dict[p] for p in params_list if p in best_params_dict}
                all_strategies[regime][strat_name] = params_for_strat

    with open(optimal_settings_file, 'w', encoding='utf-8') as f:
        json.dump(_to_jsonable_dict(all_settings), f, indent=4, ensure_ascii=False)
    with open(strategies_optimized_file, 'w', encoding='utf-8') as f:
        json.dump(_to_jsonable_dict(all_strategies), f, indent=2, ensure_ascii=False)
    
    print(f"\n\n💾 최종 파라미터 저장 완료 → {optimal_settings_file}, {strategies_optimized_file}")

    # ▼▼▼ [요청사항 반영] 모든 테스트 종료 후 최종 결과 요약 출력 ▼▼▼
    print(f"\n\n{'='*70}\n🏆 Walk-Forward 최종 검증 결과 요약\n{'='*70}")
    for symbol, results in all_validation_results.items():
        if not results: continue
        print(f"\n---Symbol: {symbol} ---")
        total_return = 0
        total_trades = 0
        for res in results:
            print(f"  - 기간: {res['period']}, 국면: {res['regime']:<8} | Return: {res['return_pct']:.2f}%, Trades: {res['trades']}")
            total_return += res['return_pct']
            total_trades += res['trades']
        avg_return = total_return / len(results) if results else 0
        print(f"  --------------------")
        print(f"  => 평균 수익률: {avg_return:.2f}%  |  총 거래 수: {total_trades}")
    print(f"\n{'='*70}")
    # ▲▲▲ [요청사항 반영] 종료 ▲▲▲
