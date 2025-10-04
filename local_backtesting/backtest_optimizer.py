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
from core.config_manager import config

# (선택형 최적화기)
try:
    from local_backtesting.optimizers import run_ga, run_bayes
    _HAS_OPTIMIZERS = True
except Exception:
    _HAS_OPTIMIZERS = False


# ---- 안전 폴백: 전략 설정 읽기 ----
def get_strategy_configs_safe(regime: str):
    """
    ConfigManager가 get_strategy_configs를 제공하지 않는 경우를 대비한 안전 래퍼.
    """
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
def collapse_regimes_to_periods(df: pd.DataFrame, macro_data: dict) -> dict:
    """
    df(index: DatetimeIndex, cols: Open/High/Low/Close/Volume)에 대해
    MacroAnalyzer로 시점별 레짐을 계산하고 동일 레짐 연속 구간을 (start,end)로 압축.
    너무 짧은 구간(4h 300봉 ≈ 50일) 제거.
    """
    ma = MacroAnalyzer()
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

    # ====== 실행정책(기본값, bt.run으로 덮임) ======
    open_threshold = 12.0
    risk_reward_ratio = 2.0
    sl_atr_multiplier = 1.5
    trend_entry_confirm_count = 3

    # 실행정책 확장
    exec_partial = "1.0"                # "1.0" 또는 "0.3,0.3,0.4"
    exec_time_stop_bars = 0             # 0이면 비활성
    exec_trailing_mode = "off"          # "off"|"atr"|"percent"
    exec_trailing_k = 0.0               # atr배수 또는 percent

    # ====== 리스크 사이징(상대 크기) ======
    risk_per_trade = 0.01               # 자본 1%
    max_exposure_frac = 0.30            # 자본 대비 최대 상대 노출(마진 전)

    # ====== 분석 파라미터 ======
    ema_short = 20
    ema_long = 50
    score_strong_trend = 5
    rsi_period = 14
    rsi_oversold = 30
    rsi_overbought = 70
    score_oversold = 5
    score_overbought = -5
    score_macd_cross_up = 2
    adx_threshold = 25
    score_adx_strong = 3
    score_bb_breakout_up = 4
    score_chop_trending = 3

    # (컨텍스트)
    symbol = "BTCUSDT"
    market_regime = "BULL"

    # ====== 상태 ======
    _recent_scores: deque
    _in_pos: bool
    _side: str
    _entry_px: float
    _entry_atr: float
    _sl_px: float
    _tp_plan: list
    _bars_held: int

    def init(self):
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
                "rsi_overbought": int(self.rsi_overbought),
                "score_oversold": int(self.score_oversold),
                "score_overbought": int(self.score_overbought),
                "stoch_k": 14, "stoch_d": 3, "stoch_smooth_k": 3,
                "mfi_period": 14, "obv_ema_period": 20,
                "stoch_oversold": 20, "stoch_overbought": 80,
                "mfi_oversold": 20, "mfi_overbought": 80,
                "score_inflow": 2, "score_outflow": -2,
            },
            "ComprehensiveStrategy": {
                "enabled": True,
                "score_macd_cross_up": int(self.score_macd_cross_up),
                "score_macd_cross_down": -int(self.score_macd_cross_up),
                "adx_threshold": int(self.adx_threshold),
                "score_adx_strong": int(self.score_adx_strong),
                "score_bb_breakout_up": int(self.score_bb_breakout_up),
                "score_bb_breakout_down": -int(self.score_bb_breakout_up),
                "score_chop_trending": int(self.score_chop_trending),
                "score_ichimoku_bull": 4, "score_ichimoku_bear": -4,
                "score_psar_bull": 3, "score_psar_bear": -3,
                "score_vortex_bull": 2, "score_vortex_bear": -2,
                "bb_len": 20, "bb_std": 2.0, "score_bb_squeeze": 3,
                "cci_length": 20, "cci_constant": 0.015,
                "cci_overbought": 100, "cci_oversold": -100,
                "score_cci_overbought": -3, "score_cci_oversold": 3,
                "score_cmf_positive": 2, "score_cmf_negative": -2,
                "chop_sideways_th": 60, "score_chop_sideways": -3,
                "stochrsi_oversold": 20, "stochrsi_overbought": 80,
                "score_stochrsi_oversold": 3, "score_stochrsi_overbought": -3,
                "score_trix_cross_up": 4, "score_trix_cross_down": -4,
                "score_efi_cross_up": 3, "score_efi_cross_down": -3,
                "score_kc_breakout_up": 4, "score_kc_breakout_down": -4,
                "score_ppo_bull": 2, "score_ppo_bear": -2,
            },
        }
        self.engine = ConfluenceEngine(Client("", ""), strategy_configs=strategy_configs)

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
    """공통 목표함수용 런너. 선호: Calmar → Sharpe → Return (가드 포함)"""
    # 전략 컨텍스트
    OptoRunner.symbol = symbol
    OptoRunner.market_regime = regime

    bt = FractionalBacktest(
        df_capitalized,
        OptoRunner,
        cash=initial_cash,
        commission=.002,
        margin=1 / 10,           # 10x 레버리지
        finalize_trades=True,
    )
    stats = bt.run(**params)

    # === 동적 min_trades 완화 ===
    min_trades_env = int(os.getenv("OPT_MIN_TRADES", 50))
    dataset_len = len(df_capitalized) if hasattr(df_capitalized, "__len__") else 0
    min_trades_dyn = max(10, dataset_len // 100)  # 대략 100봉당 1건, 하한 10
    min_trades = min(min_trades_env, min_trades_dyn)

    mdd_floor = float(os.getenv("OPT_MDD_FLOOR_PCT", 3.0))

    def _f(x, default=float("nan")):
        try:
            return float(x)
        except Exception:
            return default

    def _finite(x):
        return (x is not None) and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))

    trades = int(stats.get("# Trades", 0) or 0)
    mdd = abs(_f(stats.get("Max. Drawdown [%]", 0), 0.0))

    calmar = _f(stats.get("Calmar Ratio"))
    sharpe = _f(stats.get("Sharpe Ratio"))
    retpct = _f(stats.get("Return [%]"), 0.0)

    if trades < min_trades:
        return stats, -1e12, f"Rejected: few trades (<{min_trades})"

    if mdd < mdd_floor:
        if _finite(sharpe):
            return stats, float(sharpe), "Sharpe Ratio (fallback)"
        return stats, float(retpct), "Return [%] (fallback)"

    if _finite(calmar):
        return stats, float(calmar), "Calmar Ratio"
    if _finite(sharpe):
        return stats, float(sharpe), "Sharpe Ratio"
    return stats, float(retpct), "Return [%]"


def get_param_spaces():
    """탐색공간(그리드/GA/베이지안 공통)"""
    return {
        # 분석/임계
        "open_threshold":       {"type":"int",   "choices":[10,12,14,16]},
        "risk_reward_ratio":    {"type":"float", "choices":[1.8,2.0,2.5,3.0]},
        "sl_atr_multiplier":    {"type":"float", "choices":[1.2,1.5,1.8,2.2]},
        "trend_entry_confirm_count":{"type":"int","choices":[2,3,4]},
        "ema_short":            {"type":"int",   "choices":[12,16,20,24]},
        "ema_long":             {"type":"int",   "choices":[40,50,60,80]},
        "score_strong_trend":   {"type":"int",   "choices":[3,4,5]},
        "rsi_period":           {"type":"int",   "choices":[14]},
        "rsi_oversold":         {"type":"int",   "choices":[20,25,30]},
        "score_oversold":       {"type":"int",   "choices":[3,4,5]},
        "score_macd_cross_up":  {"type":"int",   "choices":[2,3,4]},
        "adx_threshold":        {"type":"int",   "choices":[18,22,25,28]},
        "score_adx_strong":     {"type":"int",   "choices":[2,3,4]},
        # 실행정책
        "exec_partial":         {"type":"cat",   "choices":["1.0","0.3,0.3,0.4"]},
        "exec_time_stop_bars":  {"type":"int",   "choices":[0,8,12,16]},
        "exec_trailing_mode":   {"type":"cat",   "choices":["off","atr","percent"]},
        "exec_trailing_k":      {"type":"float", "choices":[0.0,1.0,1.5,2.0]},
        # 리스크 사이징(상대 크기)
        "risk_per_trade":       {"type":"float", "choices":[0.005,0.01,0.015,0.02]},
        "max_exposure_frac":    {"type":"float", "choices":[0.2,0.3,0.4]},
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


if __name__ == '__main__':
    backtesting.Pool = multiprocessing.Pool

    symbols_to_optimize = ["BTCUSDT", "ETHUSDT"]
    initial_cash = 10_000

    # Binance 클라이언트 (공개엔드포인트)
    binance_client = Client(
        getattr(config, "api_key", "") or "",
        getattr(config, "api_secret", "") or ""
    )

    # 결과 파일
    optimal_settings_file = os.path.join(project_root, "optimal_settings.json")
    strategies_optimized_file = os.path.join(project_root, "strategies_optimized.json")

    try:
        with open(optimal_settings_file, 'r', encoding='utf-8') as f:
            all_settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        all_settings = {}

    try:
        with open(strategies_optimized_file, 'r', encoding='utf-8') as f:
            all_strategies = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        all_strategies = {"BULL": {}, "BEAR": {}, "SIDEWAYS": {}}

    # 매크로 데이터 프리로드
    ma = MacroAnalyzer()
    macro_preloaded = ma.preload_all_macro_data()

    # 최적화 방법 결정
    param_spaces = get_param_spaces()
    method = choose_method_auto(param_spaces)
    print(f"\n[OPT] 선택된 최적화 알고리즘: {method.upper()}  "
          f"(ENV OPT_METHOD={os.getenv('OPT_METHOD','auto')})")

    for symbol in symbols_to_optimize:
        print(f"\n\n{'='*68}\n📦 전체 데이터 로드: {symbol} (since 2018-01-01)\n{'='*68}")
        # 2018년부터 전구간 캔들 확보
        klines = fetch_klines_since_2018(binance_client, symbol, "4h")
        if klines is None or len(klines) < 500:
            print(f"[SKIP] {symbol} 데이터 부족")
            continue

        # 레짐을 연속 구간으로 압축
        periods_by_regime = collapse_regimes_to_periods(klines, macro_preloaded)

        # 폴백: 매크로 비어있으면 EMA200/MACD
        if not periods_by_regime["BULL"] and not periods_by_regime["BEAR"]:
            print("⚠️ 매크로 periods 비어있음 → EMA200/MACD 폴백으로 구간 작성")
            df_tmp = klines.copy()
            ema200 = df_tmp["Close"].ewm(span=200, adjust=False).mean()
            ema12 = df_tmp["Close"].ewm(span=12, adjust=False).mean()
            ema26 = df_tmp["Close"].ewm(span=26, adjust=False).mean()
            macd = ema12 - ema26

            def _mask_to_periods(mask_ser):
                periods = []
                run = None
                prev_t = None
                for t, m in mask_ser.items():
                    if m and run is None:
                        run = {"start": t}
                    if (not m) and run is not None:
                        run["end"] = prev_t
                        periods.append(run)
                        run = None
                    prev_t = t
                if run is not None:
                    run["end"] = mask_ser.index[-1]
                    periods.append(run)
                # 최소 길이 필터(300 bars)
                out = []
                MIN_BARS = 300
                for p in periods:
                    mask = (mask_ser.index >= p["start"]) & (mask_ser.index <= p["end"])
                    if mask.sum() >= MIN_BARS:
                        out.append(p)
                return out

            mask_bull = (df_tmp["Close"] > ema200) & (macd > 0)
            periods_by_regime["BULL"] = _mask_to_periods(mask_bull)
            periods_by_regime["BEAR"] = _mask_to_periods(~mask_bull)

        # 에피소드별 최적화
        for regime in ["BULL", "BEAR"]:
            episodes = periods_by_regime.get(regime, [])
            if not episodes:
                print(f"[SKIP] {symbol}/{regime}: 매크로 에피소드가 없습니다.")
                continue

            print(f"\n--- 🔬 [{symbol}] '{regime}' 에피소드 {len(episodes)}개 최적화 ---")
            for ep_idx, ep in enumerate(episodes, start=1):
                s_ts = pd.to_datetime(ep["start"])
                e_ts = pd.to_datetime(ep["end"])
                mask = (klines.index >= s_ts) & (klines.index <= e_ts)
                df = klines.loc[mask]
                if df is None or len(df) < 300:
                    print(f"[SKIP] '{regime}' 에피소드#{ep_idx} 데이터 부족 ({s_ts.date()}~{e_ts.date()})")
                    continue

                df = df.copy()
                df.columns = [c.capitalize() for c in df.columns]

                print(
                    f"\n{'-'*60}\n"
                    f"⏱️ [{symbol} | {SYMBOL_NAME.get(symbol, symbol)}] "
                    f"{REGIME_NAME.get(regime, regime)} 에피소드 #{ep_idx}\n"
                    f"    기간: {s_ts.date()} → {e_ts.date()}  |  캔들 수: {len(df)}\n"
                    f"{'-'*60}"
                )

                # === 최적화 분기 ===
                if method == "grid":
                    OptoRunner.symbol = symbol
                    OptoRunner.market_regime = regime
                    bt = FractionalBacktest(
                        df, OptoRunner,
                        cash=initial_cash, commission=.002, margin=1/10,
                        finalize_trades=True
                    )
                    stats = bt.optimize(
                        # 분석/임계
                        open_threshold=[10, 12, 14, 16],
                        risk_reward_ratio=[1.8, 2.0, 2.5, 3.0],
                        sl_atr_multiplier=[1.2, 1.5, 1.8, 2.2],
                        trend_entry_confirm_count=[2, 3, 4],
                        ema_short=[12, 16, 20, 24],
                        ema_long=[40, 50, 60, 80],
                        score_strong_trend=[3, 4, 5],
                        rsi_oversold=[20, 25, 30],
                        score_oversold=[3, 4, 5],
                        rsi_period=[14],
                        score_macd_cross_up=[2, 3, 4],
                        adx_threshold=[18, 22, 25, 28],
                        score_adx_strong=[2, 3, 4],
                        # 실행정책
                        exec_partial=["1.0", "0.3,0.3,0.4"],
                        exec_time_stop_bars=[0, 8, 12, 16],
                        exec_trailing_mode=["off", "atr", "percent"],
                        exec_trailing_k=[0.0, 1.0, 1.5, 2.0],
                        # 리스크 사이징
                        risk_per_trade=[0.005, 0.01, 0.015, 0.02],
                        max_exposure_frac=[0.2, 0.3, 0.4],
                        maximize='Calmar Ratio',
                        constraint=lambda p: p.ema_short < p.ema_long and p.risk_reward_ratio > p.sl_atr_multiplier
                    )
                    best_params = stats._strategy
                    metric_name = 'Calmar Ratio'
                    metric_value = float(stats[metric_name]) if metric_name in stats and pd.notna(stats[metric_name]) else 0.0

                elif method in ("ga", "bayes") and _HAS_OPTIMIZERS:
                    param_spaces = get_param_spaces()
                    def objective(eval_params: dict) -> float:
                        snapped = {}
                        for k, s in param_spaces.items():
                            v = eval_params.get(k)
                            ch = s.get("choices")
                            if ch:
                                v = v if v in ch else ch[0]
                            snapped[k] = v
                        if snapped.get("ema_short", 0) >= snapped.get("ema_long", 1):
                            return -1e12
                        if snapped.get("risk_reward_ratio", 0) <= snapped.get("sl_atr_multiplier", 0):
                            return -1e12
                        _, score, _ = run_backtest_with_params(df, snapped, initial_cash, symbol, regime)
                        return score  # 큰 값이 좋음

                    if method == "ga":
                        best_params_dict, metric_value = run_ga(objective, param_spaces)
                    else:
                        import numpy as _np
                        def objective_min(eval_params: dict) -> float:
                            s = objective(eval_params)
                            return -float(s) if (s is not None and _np.isfinite(s)) else 1e12
                        best_params_dict, metric_value_min = run_bayes(objective_min, param_spaces)
                        metric_value = -float(metric_value_min)

                    class _Wrap: ...
                    best_params = _Wrap()
                    for k, v in best_params_dict.items():
                        setattr(best_params, k, v)
                    metric_name = "Objective"

                else:
                    # 폴백: grid
                    OptoRunner.symbol = symbol
                    OptoRunner.market_regime = regime
                    bt = FractionalBacktest(
                        df, OptoRunner,
                        cash=initial_cash, commission=.002, margin=1/10,
                        finalize_trades=True
                    )
                    stats = bt.optimize(
                        open_threshold=[10, 12, 14, 16],
                        risk_reward_ratio=[1.8, 2.0, 2.5, 3.0],
                        sl_atr_multiplier=[1.2, 1.5, 1.8, 2.2],
                        trend_entry_confirm_count=[2, 3, 4],
                        ema_short=[12, 16, 20, 24],
                        ema_long=[40, 50, 60, 80],
                        score_strong_trend=[3, 4, 5],
                        rsi_oversold=[20, 25, 30],
                        score_oversold=[3, 4, 5],
                        rsi_period=[14],
                        score_macd_cross_up=[2, 3, 4],
                        adx_threshold=[18, 22, 25, 28],
                        score_adx_strong=[2, 3, 4],
                        exec_partial=["1.0", "0.3,0.3,0.4"],
                        exec_time_stop_bars=[0, 8, 12, 16],
                        exec_trailing_mode=["off", "atr", "percent"],
                        exec_trailing_k=[0.0, 1.0, 1.5, 2.0],
                        risk_per_trade=[0.005, 0.01, 0.015, 0.02],
                        max_exposure_frac=[0.2, 0.3, 0.4],
                        maximize='Calmar Ratio',
                        constraint=lambda p: p.ema_short < p.ema_long and p.risk_reward_ratio > p.sl_atr_multiplier
                    )
                    best_params = stats._strategy
                    metric_name = 'Calmar Ratio'
                    metric_value = float(stats[metric_name]) if metric_name in stats and pd.notna(stats[metric_name]) else 0.0

                print(
                    f"\n--- ✅ [{symbol} | {SYMBOL_NAME.get(symbol, symbol)}] "
                    f"{REGIME_NAME.get(regime, regime)} 에피소드 #{ep_idx} 최적화 완료! "
                    f"(평가지표: {metric_name} = {metric_value:.3f}) ---"
                )

                # === 공통: 베스트 파라미터 재평가 + 리포트/로그 + 저장 ===
                best_kv = {k: getattr(best_params, k) for k in BEST_PARAM_KEYS if hasattr(best_params, k)}
                print("   📊 Best Params:", json.dumps(_to_jsonable_dict(best_kv), ensure_ascii=False))
                print(f"   🏆 {metric_name}: {metric_value:.4f}")

                # 재평가
                bt_eval = FractionalBacktest(
                    df, OptoRunner,
                    cash=initial_cash, commission=.002, margin=1/10,
                    finalize_trades=True
                )
                best_kwargs = {k: getattr(best_params, k) for k in BEST_PARAM_KEYS if hasattr(best_params, k)}
                stats_eval = bt_eval.run(**best_kwargs)

                def _g(name, default=0.0):
                    try:
                        v = stats_eval.get(name, default)
                        return float(v) if v is not None else default
                    except Exception:
                        return default

                trades = int(stats_eval.get("# Trades", 0) or 0)
                wins = int(stats_eval.get("# Winning Trades", 0) or 0)
                winrate = (wins / trades * 100.0) if trades else 0.0
                ret_pct = _g("Return [%]")
                cagr = _g("Return (Ann.) [%]")
                mdd = _g("Max. Drawdown [%]")
                pf = _g("Profit Factor")
                exposure = _g("Exposure Time [%]")
                calmar = stats_eval.get("Calmar Ratio", None)
                sharpe = stats_eval.get("Sharpe Ratio", None)

                print(
                    f"   ── 성과 요약 (재평가) │ {symbol} {SYMBOL_NAME.get(symbol, symbol)} │ "
                    f"{REGIME_NAME.get(regime, regime)} ─────────────"
                )
                print(f"   • 총수익률: {ret_pct:.2f}%  |  연환산수익률: {cagr:.2f}%  |  최대낙폭: {mdd:.2f}%")
                print(f"   • 승률: {winrate:.2f}%       |  수익요인(PF): {pf:.3f}     |  거래수: {trades}")
                print(f"   • 노출시간: {exposure:.2f}% |  칼마비율: {calmar}        |  샤프지수: {sharpe}")

                # === HTML 리포트 저장 (local_backtesting/results/<SYMBOL>/...) ===
                results_root = os.path.join(os.path.dirname(__file__), "results", symbol)
                os.makedirs(results_root, exist_ok=True)
                tag = f"{s_ts.date()}_{e_ts.date()}"
                html_path = os.path.join(results_root, f"{symbol}_{regime}_{tag}_best.html")
                try:
                    bt_eval.plot(open_browser=False, filename=html_path)
                    print(
                        f"   🧾 리포트 저장 완료: {html_path}  "
                        f"({symbol} {SYMBOL_NAME.get(symbol, symbol)} | {REGIME_NAME.get(regime, regime)} | 에피소드 #{ep_idx})"
                    )

                except Exception as e:
                    print(f"   [WARN] HTML plot failed: {e}")

                # ===== 결과 저장(JSON) =====
                all_settings.setdefault(f"{regime}", {}).setdefault(symbol, {})
                all_settings[regime][symbol][tag] = {
                    **{
                        "OPEN_TH": int(getattr(best_params, "open_threshold")),
                        "RR_RATIO": float(getattr(best_params, "risk_reward_ratio")),
                        "SL_ATR_MULTIPLIER": float(getattr(best_params, "sl_atr_multiplier")),
                        "TREND_ENTRY_CONFIRM_COUNT": int(getattr(best_params, "trend_entry_confirm_count")),
                        # 실행정책
                        "exec_partial": getattr(best_params, "exec_partial", "1.0"),
                        "exec_time_stop_bars": int(getattr(best_params, "exec_time_stop_bars", 0)),
                        "exec_trailing_mode": getattr(best_params, "exec_trailing_mode", "off"),
                        "exec_trailing_k": float(getattr(best_params, "exec_trailing_k", 0.0)),
                        # 리스크 사이징
                        "risk_per_trade": float(getattr(best_params, "risk_per_trade", 0.01)),
                        "max_exposure_frac": float(getattr(best_params, "max_exposure_frac", 0.30)),
                        "OPTIMIZED_METRIC": metric_name,
                        "VALUE": float(round(metric_value or 0.0, 4)),
                    },
                    "SUMMARY": {
                        "Return_%": round(ret_pct, 4),
                        "CAGR_%": round(cagr, 4),
                        "MaxDD_%": round(mdd, 4),
                        "WinRate_%": round(winrate, 4),
                        "ProfitFactor": round(pf, 4),
                        "Exposure_%": round(exposure, 4),
                        "Calmar": None if (calmar is None or (isinstance(calmar,float) and (math.isnan(calmar) or math.isinf(calmar)))) else round(float(calmar), 4),
                        "Sharpe": None if (sharpe is None or (isinstance(sharpe,float) and (math.isnan(sharpe) or math.isinf(sharpe)))) else round(float(sharpe), 4),
                        "Trades": trades,
                        "Period": {"start": s_ts.isoformat(), "end": e_ts.isoformat()}
                    }
                }
                with open(optimal_settings_file, 'w', encoding='utf-8') as f:
                    json.dump(all_settings, f, indent=4, ensure_ascii=False)

                # (2) 전략 점수/지표 파라미터 저장
                base_strategies = get_strategy_configs_safe(regime)
                base_strategies = json.loads(json.dumps(base_strategies))  # deep copy
                base_strategies.setdefault("TrendStrategy", {})
                base_strategies.setdefault("OscillatorStrategy", {})
                base_strategies.setdefault("ComprehensiveStrategy", {})

                base_strategies["TrendStrategy"]["ema_short"] = int(getattr(best_params, "ema_short"))
                base_strategies["TrendStrategy"]["ema_long"] = int(getattr(best_params, "ema_long"))
                base_strategies["TrendStrategy"]["score_strong_trend"] = int(getattr(best_params, "score_strong_trend"))

                base_strategies["OscillatorStrategy"]["rsi_period"] = int(getattr(best_params, "rsi_period"))
                rsi_os = int(getattr(best_params, "rsi_oversold"))
                base_strategies["OscillatorStrategy"]["rsi_oversold"] = rsi_os
                base_strategies["OscillatorStrategy"]["rsi_overbought"] = 100 - rsi_os
                soc_os = int(getattr(best_params, "score_oversold"))
                base_strategies["OscillatorStrategy"]["score_oversold"] = soc_os
                base_strategies["OscillatorStrategy"]["score_overbought"] = -soc_os

                base_strategies["ComprehensiveStrategy"]["score_macd_cross_up"] = int(getattr(best_params, "score_macd_cross_up"))
                base_strategies["ComprehensiveStrategy"]["score_macd_cross_down"] = -int(getattr(best_params, "score_macd_cross_up"))
                base_strategies["ComprehensiveStrategy"]["adx_threshold"] = int(getattr(best_params, "adx_threshold"))
                base_strategies["ComprehensiveStrategy"]["score_adx_strong"] = int(getattr(best_params, "score_adx_strong"))

                all_strategies[regime] = base_strategies or {}
                with open(strategies_optimized_file, 'w', encoding='utf-8') as f:
                    json.dump(all_strategies, f, indent=2, ensure_ascii=False)

                print(f"   💾 저장 완료 → {optimal_settings_file}, {strategies_optimized_file}")
