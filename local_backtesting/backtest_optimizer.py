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


def _to_jsonable_dict(d: dict) -> dict:
    def conv(x):
        try:
            import numpy as np  # noqa
            if isinstance(x, (np.integer,)):
                return int(x)
            if isinstance(x, (np.floating,)):
                return float(x)
            if isinstance(x, (np.bool_,)):
                return bool(x)
        except Exception:
            pass
        if isinstance(x, pd.Timestamp):
            return x.isoformat()
        if isinstance(x, (int, float, bool, str)) or x is None:
            return x
        try:
            return float(x)
        except Exception:
            try:
                return int(x)
            except Exception:
                return str(x)
    return {k: conv(v) for k, v in d.items()}


# === 신규: 2018-01-01부터 4h 전구간 수집 ===
def fetch_klines_since_2018(binance_client: Client, symbol: str, interval: str = "4h") -> pd.DataFrame:
    """
    Binance SDK의 get_historical_klines로 2018-01-01 UTC부터 전구간 수집.
    """
    start_str = "1 Jan, 2018"
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
def collapse_regimes_to_periods(df: pd.DataFrame, macro_data: dict, config: ConfigManager) -> dict:
    """
    df(index: DatetimeIndex, cols: Open/High/Low/Close/Volume)에 대해
    MacroAnalyzer로 시점별 레짐을 계산하고 동일 레짐 연속 구간을 (start,end)로 압축.
    너무 짧은 구간(4h 300봉 ≈ 50일) 제거.
    """
    ma = MacroAnalyzer(config) # config 객체를 전달하여 MacroAnalyzer 생성
    regimes = []
    for ts in tqdm(df.index, desc="레짐 레이블링"):
        regime, _, _ = ma.diagnose_macro_regime_for_date(ts, macro_data)
        regimes.append(regime.name if isinstance(regime, MacroRegime) else str(regime))
    ser = pd.Series(regimes, index=df.index, name="Regime")

    periods = {"BULL": [], "BEAR": [], "SIDEWAYS": []}
    if ser.empty:
        return periods

    prev = ser.iloc[0]
    run_start = ser.index[0]
    # 연속 구간 찾기
    for i in range(1, len(ser)):
        t = ser.index[i]
        r = ser.iloc[i]
        if r != prev:
            periods.setdefault(prev, []).append({"start": run_start, "end": ser.index[i-1]})
            run_start = t
            prev = r
    periods.setdefault(prev, []).append({"start": run_start, "end": ser.index[-1]})

    # 최소 길이 필터
    MIN_BARS = 300
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

    # ====== 실행정책(최적화 대상) ======
    open_threshold = 12.0
    risk_reward_ratio = 2.0
    sl_atr_multiplier = 1.5
    trend_entry_confirm_count = 3
    risk_per_trade = 0.01
    
    # ====== TrendStrategy 파라미터(최적화 대상) ======
    ema_short = 20
    ema_long = 50
    score_strong_trend = 5
    
    # ====== OscillatorStrategy 파라미터(최적화 대상) ======
    rsi_period = 14
    rsi_oversold = 30
    score_oversold = 5
    
    # --- ▼▼▼ [핵심 수정] 누락된 ComprehensiveStrategy 파라미터 추가 ▼▼▼ ---
    score_macd_cross_up = 2
    adx_threshold = 25
    score_adx_strong = 3
    score_bb_breakout_up = 4
    score_chop_trending = 3
    stochrsi_oversold = 20
    score_stochrsi_oversold = 3
    score_trix_cross_up = 4
    # --- ▲▲▲ [핵심 수정] ---

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
                "enabled": True,
                "ema_short": int(self.ema_short),
                "ema_long": int(self.ema_long),
                "score_strong_trend": int(self.score_strong_trend),
            },
            "OscillatorStrategy": {
                "enabled": True,
                "rsi_period": int(self.rsi_period),
                "rsi_oversold": int(self.rsi_oversold),
                "rsi_overbought": 100 - int(self.rsi_oversold),      # 동적으로 계산
                "score_oversold": int(self.score_oversold),
                "score_overbought": -int(self.score_oversold),     # 동적으로 계산
                # (이하 기본값 유지)
                "stoch_k": 14, "stoch_d": 3, "stoch_smooth_k": 3,
                "mfi_period": 14, "obv_ema_period": 20,
                "stoch_oversold": 20, "stoch_overbought": 80,
                "mfi_oversold": 20, "mfi_overbought": 80,
                "score_inflow": 2, "score_outflow": -2,
            },
            "ComprehensiveStrategy": {
                "enabled": True,
                "score_macd_cross_up": int(self.score_macd_cross_up),
                "score_macd_cross_down": -int(self.score_macd_cross_up), # 동적으로 계산
                "adx_threshold": int(self.adx_threshold),
                "score_adx_strong": int(self.score_adx_strong),
                "score_bb_breakout_up": int(self.score_bb_breakout_up),
                "score_bb_breakout_down": -int(self.score_bb_breakout_up), # 동적으로 계산
                "score_chop_trending": int(self.score_chop_trending),
                "stochrsi_oversold": int(self.stochrsi_oversold),
                "stochrsi_overbought": 100 - int(self.stochrsi_oversold), # 동적으로 계산
                "score_stochrsi_oversold": int(self.score_stochrsi_oversold),
                "score_stochrsi_overbought": -int(self.score_stochrsi_oversold), # 동적으로 계산
                "score_trix_cross_up": int(self.score_trix_cross_up),
                "score_trix_cross_down": -int(self.score_trix_cross_up), # 동적으로 계산
                # (이하 기본값 유지)
                "score_ichimoku_bull": 4, "score_ichimoku_bear": -4,
                "score_psar_bull": 3, "score_psar_bear": -3,
                "score_vortex_bull": 2, "score_vortex_bear": -2,
                "bb_len": 20, "bb_std": 2.0, "score_bb_squeeze": 3,
                "cci_length": 20, "cci_constant": 0.015,
                "cci_overbought": 100, "cci_oversold": -100,
                "score_cci_overbought": -3, "score_cci_oversold": 3,
                "score_cmf_positive": 2, "score_cmf_negative": -2,
                "chop_sideways_th": 60, "score_chop_sideways": -3,
                "score_efi_cross_up": 3, "score_efi_cross_down": -3,
                "score_kc_breakout_up": 4, "score_kc_breakout_down": -4,
                "score_ppo_bull": 2, "score_ppo_bear": -2,
            },
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
        idx = len(self.data) - 1
        cur = self.indicators.iloc[:idx + 1]
        if len(cur) < int(self.trend_entry_confirm_count):
            return

        current_score, _ = self.engine._calculate_tactical_score(cur)
        self._recent_scores.append(current_score)
        if len(self._recent_scores) < int(self.trend_entry_confirm_count):
            return

        avg_score = sum(self._recent_scores) / len(self._recent_scores)

        # 진입 판단
        side = None
        if self.market_regime == "BULL" and avg_score >= float(self.open_threshold):
            side = "BUY"
        elif self.market_regime == "BEAR" and avg_score <= -float(self.open_threshold):
            side = "SELL"

        if (not self._in_pos) and side:
            self._maybe_enter(side)

        # 보유 중 관리
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
def run_backtest_with_params(
    df_capitalized: pd.DataFrame,
    params: dict,
    initial_cash: int,
    symbol: str,
    regime: str
):
    """
    강화된 평가 로직: Calmar 우선, 최소 거래 횟수, 수익 곡선 안정성 평가.
    """
    # 전략 컨텍스트 설정
    OptoRunner.symbol = symbol
    OptoRunner.market_regime = regime

    bt = FractionalBacktest(
        df_capitalized,
        OptoRunner,
        cash=initial_cash,
        commission=.002,
        margin=1 / 10,
    )
    stats = bt.run(**params)

    # 1. 최소 거래 횟수 검증 (데이터 길이에 따라 동적 조절)
    min_trades = max(30, len(df_capitalized) // 200) # 200봉당 1회, 최소 30회
    trades = int(stats.get("# Trades", 0) or 0)
    if trades < min_trades:
        # 거래 횟수 미달 시, 횟수만큼 점수를 깎아 페널티 부여
        return stats, -1e9 + trades, f"거래 부족(>{min_trades}회)"

    # 2. 핵심 지표 추출
    calmar = float(stats.get("Calmar Ratio", 0) or 0)
    sharpe = float(stats.get("Sharpe Ratio", 0) or 0)
    
    # 3. 수익 곡선 안정성 평가 (선형 회귀 기울기)
    equity_curve = stats.get("_equity_curve", {}).get("Equity", pd.Series(dtype=float))
    if not equity_curve.empty and len(equity_curve) > 1:
        x = np.arange(len(equity_curve))
        y = equity_curve.values
        # polyfit으로 기울기 계산
        slope, _ = np.polyfit(x, y, 1)
        # 기울기를 점수화하여 안정성 보너스로 반영
        stability_bonus = (slope / initial_cash) * 1000
    else:
        stability_bonus = 0

    # 4. 최종 점수 계산 (Calmar 우선, Sharpe는 50% 가중치, 안정성 보너스 추가)
    primary_metric = calmar if not pd.isna(calmar) and calmar > 0 else (sharpe * 0.5)
    final_score = primary_metric + stability_bonus

    return stats, final_score, "Calmar + Stability"


def get_param_spaces():
    """확장된 탐색 공간: ComprehensiveStrategy의 주요 지표 포함"""
    return {
        # 실행정책
        "open_threshold":       {"type":"int", "choices":[10, 12, 14, 16]},
        "risk_reward_ratio":    {"type":"float", "low":1.5, "high":3.0},
        "sl_atr_multiplier":    {"type":"float", "low":1.2, "high":2.5},
        "trend_entry_confirm_count": {"type":"int", "choices":[2, 3, 4]},
        "risk_per_trade":       {"type":"float", "low":0.005, "high":0.02},

        # TrendStrategy
        "ema_short":            {"type":"int", "low":10, "high":25},
        "ema_long":             {"type":"int", "low":40, "high":80},
        "score_strong_trend":   {"type":"int", "choices":[3, 4, 5]},

        # OscillatorStrategy
        "rsi_period":           {"type":"int", "choices":[14]},
        "rsi_oversold":         {"type":"int", "low":20, "high":35},
        "score_oversold":       {"type":"int", "choices":[3, 4, 5]},

        # ComprehensiveStrategy (신규 확장)
        "score_macd_cross_up":  {"type":"int", "choices":[2, 3]},
        "adx_threshold":        {"type":"int", "low":18, "high":28},
        "score_adx_strong":     {"type":"int", "choices":[2, 3]},
        "score_bb_breakout_up": {"type":"int", "choices":[3, 4, 5]},
        "score_chop_trending":  {"type":"int", "choices":[2, 3]},
        "stochrsi_oversold":    {"type":"int", "low":15, "high":30},
        "score_stochrsi_oversold": {"type":"int", "choices":[2, 3]},
        "score_trix_cross_up":  {"type":"int", "choices":[3, 4]},
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
    from core.config_manager import ConfigManager
    config = ConfigManager()
    
    backtesting.Pool = multiprocessing.Pool

    symbols_to_optimize = ["BTCUSDT", "ETHUSDT"]
    initial_cash = 10_000

    # Binance 클라이언트 (공개엔드포인트)
    binance_client = Client(
        getattr(config, "api_key", "") or "",
        getattr(config, "api_secret", "") or ""
    )

    # 결과 파일 경로 설정
    optimal_settings_file = os.path.join(project_root, "optimal_settings.json")
    strategies_optimized_file = os.path.join(project_root, "strategies_optimized.json")

    # 기존 설정 파일 로드
    try:
        with open(optimal_settings_file, 'r', encoding='utf-8') as f: all_settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): all_settings = {}
    try:
        with open(strategies_optimized_file, 'r', encoding='utf-8') as f: all_strategies = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): all_strategies = {"BULL": {}, "BEAR": {}, "SIDEWAYS": {}}

    # --- ▼▼▼ [수정] MacroAnalyzer 생성 시 config 전달 ▼▼▼ ---
    ma = MacroAnalyzer(config)
    macro_preloaded = ma.preload_all_macro_data()
    # --- ▲▲▲ [수정] ---

    # 최적화 방법 결정
    param_spaces = get_param_spaces()
    method = "bayes" if _HAS_OPTIMIZERS else "grid"
    print(f"\n[OPT] 선택된 최적화 알고리즘: {method.upper()}")

    for symbol in symbols_to_optimize:
        print(f"\n\n{'='*70}\n📦 전체 데이터 로드 및 구간 분석: {symbol}\n{'='*70}")
        
        klines = fetch_klines_since_2018(binance_client, symbol, "4h")
        if klines is None or len(klines) < 500:
            print(f"[SKIP] {symbol} 데이터 부족")
            continue
        
        # --- ▼▼▼ [수정] collapse_regimes_to_periods 호출 시 config 전달 ▼▼▼ ---
        periods_by_regime = collapse_regimes_to_periods(klines, macro_preloaded, config)
        # --- ▲▲▲ [수정] ---

        for regime in ["BULL", "BEAR"]:
            episodes = periods_by_regime.get(regime, [])
            if not episodes:
                print(f"\n[SKIP] {symbol}/{regime}: 분석할 데이터 구간이 없습니다.")
                continue

            # 통합 데이터셋 생성
            all_dfs = [klines.loc[ep["start"]:ep["end"]] for ep in episodes]
            if not all_dfs: continue
            
            df = pd.concat(all_dfs).sort_index()
            df = df[~df.index.duplicated(keep='first')]
            df.columns = [c.capitalize() for c in df.columns]

            print(f"\n--- 🔬 [{symbol}] '{REGIME_NAME.get(regime, regime)}' 통합 데이터 최적화 시작 ---")
            print(f"    총 데이터 기간: {df.index.min().date()} ~ {df.index.max().date()}  |  캔들 수: {len(df)}")
            
            # 최적화 실행
            def objective_min(eval_params: dict) -> float:
                if eval_params.get("ema_short", 99) >= eval_params.get("ema_long", 0): return 1e9
                if eval_params.get("risk_reward_ratio", 0) <= eval_params.get("sl_atr_multiplier", 99): return 1e9
                _, score, _ = run_backtest_with_params(df, eval_params, initial_cash, symbol, regime)
                return -float(score) if pd.notna(score) else 1e9

            best_params_dict, metric_value_min = run_bayes(objective_min, param_spaces, n_calls=100)
            metric_value = -float(metric_value_min)

            print(f"\n--- ✅ [{symbol}] '{REGIME_NAME.get(regime, regime)}' 최적화 완료! (종합점수: {metric_value:.3f}) ---")
            
            # 최종 재평가 및 결과 출력/저장... (이하 로직은 기존과 동일)
            final_bt = FractionalBacktest(df, OptoRunner, cash=initial_cash, commission=.002, margin=1/10)
            stats_eval = final_bt.run(**best_params_dict)

            print(f"   📊 Best Params: {json.dumps(_to_jsonable_dict(best_params_dict), ensure_ascii=False, indent=2)}")
            print(f"   🏆 최종 성과 요약 (재평가):")
            print(f"      - Calmar Ratio: {stats_eval.get('Calmar Ratio', 'N/A'):.3f}")
            print(f"      - Return [%]: {stats_eval.get('Return [%]', 0):.2f}%")
            print(f"      - # Trades: {stats_eval.get('# Trades', 0)}")

            results_root = os.path.join(os.path.dirname(__file__), "results", symbol)
            os.makedirs(results_root, exist_ok=True)
            html_path = os.path.join(results_root, f"{symbol}_{regime}_OPTIMIZED_REPORT.html")
            try:
                final_bt.plot(open_browser=False, filename=html_path)
                print(f"   🧾 리포트 저장 완료: {html_path}")
            except Exception as e:
                print(f"   [WARN] HTML 리포트 생성 실패: {e}")

            all_settings.setdefault(regime, {})[symbol] = {
                "OPEN_TH": int(best_params_dict.get("open_threshold")),
                "RR_RATIO": float(best_params_dict.get("risk_reward_ratio")),
                "SL_ATR_MULTIPLIER": float(best_params_dict.get("sl_atr_multiplier")),
                "TREND_ENTRY_CONFIRM_COUNT": int(best_params_dict.get("trend_entry_confirm_count")),
                "risk_per_trade": float(best_params_dict.get("risk_per_trade")),
                "OPTIMIZED_METRIC_SCORE": round(metric_value, 4),
                "SUMMARY": {k: v for k, v in _to_jsonable_dict(stats_eval.to_dict()).items() if isinstance(v, (int, float, bool, str, type(None)))}
            }
            with open(optimal_settings_file, 'w', encoding='utf-8') as f:
                json.dump(all_settings, f, indent=4, ensure_ascii=False)

            base_strategies = { "TrendStrategy": {}, "OscillatorStrategy": {}, "ComprehensiveStrategy": {} }
            for k, v in best_params_dict.items():
                if k in ["ema_short", "ema_long", "score_strong_trend"]:
                    base_strategies["TrendStrategy"][k] = v
                elif k in ["rsi_period", "rsi_oversold", "score_oversold"]:
                    base_strategies["OscillatorStrategy"][k] = v
                elif k not in ["open_threshold", "risk_reward_ratio", "sl_atr_multiplier", "trend_entry_confirm_count", "risk_per_trade"]:
                     base_strategies["ComprehensiveStrategy"][k] = v
            
            if "rsi_oversold" in base_strategies["OscillatorStrategy"]:
                base_strategies["OscillatorStrategy"]["rsi_overbought"] = 100 - base_strategies["OscillatorStrategy"]["rsi_oversold"]

            all_strategies[regime] = base_strategies
            with open(strategies_optimized_file, 'w', encoding='utf-8') as f:
                json.dump(all_strategies, f, indent=2, ensure_ascii=False)
            
            print(f"   💾 저장 완료 → {optimal_settings_file}, {strategies_optimized_file}")
