# -*- coding: utf-8 -*-
# local_backtesting/backtest_optimizer.py
"""
V5 — 실행정책(부분익절/타임스탑/트레일링)까지 최적화 + 백테스트 반영

핵심 변경:
- OptoRunner: 기존 단일 SL/TP 하드코딩 삭제 → 실행정책 시뮬레이션(멀티 TP/타임스탑/트레일링) 추가
- 탐색공간에 실행정책 노브 추가(exec_partial, exec_time_stop_bars, exec_trailing_mode, exec_trailing_k)
- optimal_settings.json에 실행정책 키까지 저장 → runner가 그대로 사용
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
from tqdm import tqdm

# --- 프로젝트 경로 설정 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# .env 로드 추가
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(project_root, ".env"))  # 프로젝트 루트의 .env
except Exception:
    pass

from analysis import indicator_calculator, data_fetcher
from analysis.confluence_engine import ConfluenceEngine
from analysis.macro_analyzer import MacroAnalyzer
from core.config_manager import config

# (선택형 최적화기)
try:
    from local_backtesting.optimizers import run_ga, run_bayes
    _HAS_OPTIMIZERS = True
except Exception:
    _HAS_OPTIMIZERS = False


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


def segment_data_by_regime(klines_df: pd.DataFrame, macro_data: dict) -> dict:
    print("\n...과거 데이터 전체에 대한 거시 경제 분석을 시작합니다...")
    macro_analyzer = MacroAnalyzer()
    regime_periods = []
    for date in tqdm(klines_df.index, desc="과거 시장 상황 분석 중"):
        regime, _, _ = macro_analyzer.diagnose_macro_regime_for_date(date, macro_data)
        regime_periods.append(regime.name)
    klines_df['Regime'] = regime_periods
    segmented_data = {
        "BULL": klines_df[klines_df['Regime'] == 'BULL'],
        "BEAR": klines_df[klines_df['Regime'] == 'BEAR'],
        "SIDEWAYS": klines_df[klines_df['Regime'] == 'SIDEWAYS']
    }
    print("...거시 경제 분석 및 데이터 구간 선별 완료!")
    print(f"   - 강세장(BULL) 데이터: {len(segmented_data['BULL'])}개 캔들")
    print(f"   - 약세장(BEAR) 데이터: {len(segmented_data['BEAR'])}개 캔들")
    print(f"   - 횡보장(SIDEWAYS) 데이터: {len(segmented_data['SIDEWAYS'])}개 캔들")
    return segmented_data


class OptoRunner(Strategy):
    """
    분석(ConfluenceEngine) + 실행정책 시뮬(부분익절/타임스탑/트레일링)
    실행정책 파라미터는 bt.run(...) 인자로 주입된다.
    """

    # ====== 실행정책(기본값, bt.run으로 덮인다) ======
    open_threshold = 12.0
    risk_reward_ratio = 2.0
    sl_atr_multiplier = 1.5
    trend_entry_confirm_count = 3

    # 실행정책 확장
    exec_partial = "1.0"                # "1.0" 또는 "0.3,0.3,0.4"
    exec_time_stop_bars = 0             # 0이면 비활성
    exec_trailing_mode = "off"          # "off"|"atr"|"percent"
    exec_trailing_k = 0.0               # atr배수 또는 percent

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

    # (내부)
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

        # exec_partial 파싱
        if isinstance(self.exec_partial, str):
            parts = [p.strip() for p in self.exec_partial.split(",") if p.strip()]
            self._partials = [float(x) for x in parts] if parts else [1.0]
        elif isinstance(self.exec_partial, (list, tuple)):
            self._partials = [float(x) for x in self.exec_partial]
        else:
            self._partials = [1.0]

    # ---- 내부 유틸 ----
    def _maybe_enter(self, side: str):
        if self._in_pos:
            return
        idx = len(self.data) - 1
        cur = self.indicators.iloc[idx]
        atr = cur.get("ATRr_14", 0) or cur.get("ATR_14", 0)
        if not atr or np.isnan(atr) or atr <= 0:
            return

        px = float(self.data.Close[-1])
        sl_d = float(atr) * float(self.sl_atr_multiplier)
        rr = float(self.risk_reward_ratio)

        if side == "BUY":
            sl = px - sl_d
            tp_base = px + sl_d * rr
        else:
            sl = px + sl_d
            tp_base = px - sl_d * rr

        # 포지션 진입 (기본 1.0 유닛; Runner가 size/cash를 정함)
        if side == "BUY":
            self.buy(size=1.0)  # sl/tp는 수동 관리
        else:
            self.sell(size=1.0)

        # 상태 저장
        self._in_pos = True
        self._side = side
        self._entry_px = px
        self._entry_atr = atr
        self._sl_px = sl
        self._bars_held = 0

        # 멀티 TP 계획
        steps = [0.5, 1.0, 1.5] if len(self._partials) == 3 else [1.0] * len(self._partials)
        self._tp_plan = []
        for w, m in zip(self._partials, steps):
            tp_px = self._scale_tp(px, tp_base, side, m)
            self._tp_plan.append({"px": tp_px, "qty": float(w), "done": False})

    def _maybe_exit_by_tp(self):
        if not self._in_pos or not self._tp_plan:
            return
        last = float(self.data.Close[-1])
        for item in self._tp_plan:
            if item["done"]:
                continue
            hit = (last >= item["px"]) if self._side == "BUY" else (last <= item["px"])
            if hit:
                # 부분청산
                if self._side == "BUY":
                    self.sell(size=item["qty"])
                else:
                    self.buy(size=item["qty"])
                item["done"] = True

        # 모두 체결되면 포지션 종료 상태로 전환
        if all(x["done"] for x in self._tp_plan):
            self._reset_pos_state()

    def _maybe_exit_by_sl(self):
        if not self._in_pos:
            return
        last_low = float(self.data.Low[-1])
        last_high = float(self.data.High[-1])
        touched = (last_low <= self._sl_px) if self._side == "BUY" else (last_high >= self._sl_px)
        if touched:
            # 전량 청산
            if self._side == "BUY":
                self.position.close()  # 전량
            else:
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

        # 진입가 보호 + 트레일
        if self._side == "BUY":
            new_sl = max(self._entry_px, last - trail)
            self._sl_px = max(self._sl_px, new_sl)
        else:
            new_sl = min(self._entry_px, last + trail)
            self._sl_px = min(self._sl_px, new_sl)

    def _reset_pos_state(self):
        self._in_pos = False
        self._side = None
        self._entry_px = np.nan
        self._entry_atr = np.nan
        self._sl_px = np.nan
        self._tp_plan = []
        self._bars_held = 0

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

        # 보유 중 관리: TP/SL/타임스탑/트레일링
        if self._in_pos:
            self._maybe_trailing()
            self._maybe_exit_by_tp()
            self._maybe_exit_by_sl()
            self._maybe_time_stop()


# 결과 요약에 표시할 파라미터 키
BEST_PARAM_KEYS = [
    # 실행정책(분석 임계 포함)
    "open_threshold","risk_reward_ratio","sl_atr_multiplier","trend_entry_confirm_count",
    "exec_partial","exec_time_stop_bars","exec_trailing_mode","exec_trailing_k",
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
    공통 목표함수용 백테스트 러너.
    선호: Calmar → Sharpe → Return (가드 포함)
    """
    import os, math

    # 전략 컨텍스트
    OptoRunner.symbol = symbol
    OptoRunner.market_regime = regime

    bt = FractionalBacktest(
        df_capitalized,
        OptoRunner,
        cash=initial_cash,
        commission=.002,
        margin=1 / 10,
        finalize_trades=True,
    )
    stats = bt.run(**params)

    # 안정화 가드
    min_trades = int(os.getenv("OPT_MIN_TRADES", 50))
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
        # 실행정책(신규)
        "exec_partial":         {"type":"cat",   "choices":["1.0","0.3,0.3,0.4"]},
        "exec_time_stop_bars":  {"type":"int",   "choices":[0,8,12,16]},
        "exec_trailing_mode":   {"type":"cat",   "choices":["off","atr","percent"]},
        "exec_trailing_k":      {"type":"float", "choices":[0.0,1.0,1.5,2.0]},
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
    has_ga = _HAS_OPTIMIZERS
    has_bayes = _HAS_OPTIMIZERS
    if combos <= 3000:
        return "grid"
    if has_bayes:
        return "bayes"
    if has_ga:
        return "ga"
    return "grid"


if __name__ == '__main__':
    backtesting.Pool = multiprocessing.Pool

    symbols_to_optimize = ["BTCUSDT", "ETHUSDT"]
    initial_cash = 10_000
    # 최적화/백테스트는 인증키 불필요 → 빈 값으로 생성(공개 엔드포인트 사용)
    binance_client = Client(getattr(config, "api_key", "") or "",
                            getattr(config, "api_secret", "") or "")

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

    macro_analyzer_preload = MacroAnalyzer()
    preloaded_macro_data = macro_analyzer_preload.preload_all_macro_data()

    param_spaces = get_param_spaces()
    method = choose_method_auto(param_spaces)
    print(f"\n[OPT] 선택된 최적화 알고리즘: {method.upper()}  "
          f"(ENV OPT_METHOD={os.getenv('OPT_METHOD','auto')})")

    for symbol in symbols_to_optimize:
        print(f"\n\n{'='*56}\n🚀 {symbol} 자동 최적화 시작...\n{'='*56}")
        klines = data_fetcher.fetch_klines(binance_client, symbol, "4h", limit=1500)
        if klines is None or len(klines) < 200:
            print(f"[SKIP] {symbol} 데이터 부족")
            continue

        segmented = segment_data_by_regime(klines, preloaded_macro_data)
        
        # ⚠️ BEAR가 비면 기술적 폴백으로 생성 (Close<EMA200 & MACD<0 등)
        if segmented.get("BEAR") is not None and len(segmented["BEAR"]) == 0:
            df = klines.copy()
            # 컬럼 표준화 (소문자 → 대문자 변환 주의)
            close = df["Close"] if "Close" in df.columns else df["close"]
            ema200 = close.ewm(span=200, adjust=False).mean()
            # 간단 MACD (12-26)
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd = ema12 - ema26

            bear_mask = (close < ema200) & (macd < 0)
            bear_df = df[bear_mask]
            # 최소 샘플 보장: 200개 미만이면 가장 약한 200개를 보강
            if len(bear_df) < 200 and len(df) >= 200:
                extra = df.sort_values(by="Close").head(max(0, 200 - len(bear_df)))
                bear_df = pd.concat([bear_df, extra]).sort_index().drop_duplicates()

            if len(bear_df) > 0:
                segmented["BEAR"] = bear_df
                # SIDEWAYS에서 겹치는 부분 제거
                if "SIDEWAYS" in segmented and segmented["SIDEWAYS"] is not None and len(segmented["SIDEWAYS"]) > 0:
                    side = segmented["SIDEWAYS"].copy()
                    side = side.loc[~side.index.isin(bear_df.index)]
                    segmented["SIDEWAYS"] = side
                print(f"🛡️ 기술 폴백 적용: BEAR 캔들 {len(segmented['BEAR'])}개 생성 (EMA200 & MACD)")
            else:
                print("🛡️ 기술 폴백 실패: BEAR 조건에 해당 캔들이 충분치 않습니다.")

        # 🔁 폴백: 전부 SIDEWAYS면 가격 기반 레짐으로 임시 분할
        if len(segmented.get("BULL", [])) == 0 and len(segmented.get("BEAR", [])) == 0:
            df = klines.copy()
            df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
            bull = df[df["close"] > df["ema200"]]
            bear = df[df["close"] < df["ema200"]]
            segmented = {
                "BULL": bull,
                "BEAR": bear,
                "SIDEWAYS": df.iloc[0:0]
            }
            print("⚠️ 매크로 세그멘트 폴백 적용: EMA200 기준으로 임시 강/약세 분할")
        for regime in ["BULL", "BEAR"]:
            print(f"\n--- 🔬 [{symbol}] '{regime}' 구간 최적화 ---")
            df = segmented.get(regime)
            if df is None or len(df) < 100:
                print(f"[SKIP] '{regime}' 구간 데이터 부족")
                continue

            df = df.copy()
            df.columns = [c.capitalize() for c in df.columns]

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
                    # 실행정책(신규)
                    exec_partial=["1.0", "0.3,0.3,0.4"],
                    exec_time_stop_bars=[0, 8, 12, 16],
                    exec_trailing_mode=["off", "atr", "percent"],
                    exec_trailing_k=[0.0, 1.0, 1.5, 2.0],
                    maximize='Calmar Ratio',
                    constraint=lambda p: p.ema_short < p.ema_long and p.risk_reward_ratio > p.sl_atr_multiplier
                )
                best_params = stats._strategy
                metric_name = 'Calmar Ratio'
                metric_value = float(stats[metric_name]) if metric_name in stats and pd.notna(stats[metric_name]) else 0.0

            elif method in ("ga", "bayes") and _HAS_OPTIMIZERS:
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
                    return score

                if method == "ga":
                    best_params_dict, metric_value = run_ga(objective, param_spaces)
                else:
                    best_params_dict, metric_value = run_bayes(objective, param_spaces)

                class _Wrap: ...
                best_params_obj = _Wrap()
                for k, v in best_params_dict.items():
                    setattr(best_params_obj, k, v)
                best_params = best_params_obj
                best_kv = {k: getattr(best_params, k) for k in BEST_PARAM_KEYS if hasattr(best_params, k)}

                REPORT_HTML = os.getenv("REPORT_HTML", "on").lower() in ("1","true","on","yes")
                if REPORT_HTML:
                    rpt_params = dict(best_kv)
                    OptoRunner.symbol = symbol
                    OptoRunner.market_regime = regime
                    bt_r = FractionalBacktest(
                        df, OptoRunner,
                        cash=initial_cash, commission=.002, margin=1/10,
                        finalize_trades=True
                    )
                    _ = bt_r.run(**rpt_params)
                    out_dir = os.path.join(project_root, "reports", symbol)
                    os.makedirs(out_dir, exist_ok=True)
                    html_path = os.path.join(out_dir, f"{symbol}_{regime}_report.html")
                    try:
                        bt_r.plot(open_browser=False, filename=html_path)
                        print(f"   🧾 HTML report saved → {html_path}")
                    except Exception as e:
                        print(f"   [WARN] HTML plot failed: {e}")

                metric_name = "Calmar Ratio"

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
                    maximize='Calmar Ratio',
                    constraint=lambda p: p.ema_short < p.ema_long and p.risk_reward_ratio > p.sl_atr_multiplier
                )
                best_params = stats._strategy
                metric_name = 'Calmar Ratio'
                metric_value = float(stats[metric_name]) if metric_name in stats and pd.notna(stats[metric_name]) else 0.0

            print(f"\n--- ✅ [{symbol}/{regime}] 최적화 완료! (결과: {metric_name}={metric_value:.3f}) ---")

            # === 요약 출력 ===
            best_kv = {k: getattr(best_params, k) for k in BEST_PARAM_KEYS if hasattr(best_params, k)}
            print("   📊 Best Params:", json.dumps(_to_jsonable_dict(best_kv), ensure_ascii=False))
            print(f"   🏆 {metric_name}: {metric_value:.4f}")

            # === HTML 리포트 (grid 분기 전용) ===
            REPORT_HTML = os.getenv("REPORT_HTML", "on").lower() in ("1","true","on","yes")
            if REPORT_HTML and method == "grid":
                out_dir = os.path.join(project_root, "reports", symbol)
                os.makedirs(out_dir, exist_ok=True)
                html_path = os.path.join(out_dir, f"{symbol}_{regime}_report.html")
                try:
                    bt.plot(open_browser=False, filename=html_path)
                    print(f"   🧾 HTML report saved → {html_path}")
                except Exception as e:
                    print(f"   [WARN] HTML plot failed: {e}")

            # ===== 결과 저장 =====
            # (1) 실행 파라미터(+실행정책) 저장 — 레짐/심볼별
            if regime not in all_settings:
                all_settings[regime] = {}
            all_settings[regime][symbol] = {
                "OPEN_TH": int(getattr(best_params, "open_threshold")),
                "RR_RATIO": float(getattr(best_params, "risk_reward_ratio")),
                "SL_ATR_MULTIPLIER": float(getattr(best_params, "sl_atr_multiplier")),
                "TREND_ENTRY_CONFIRM_COUNT": int(getattr(best_params, "trend_entry_confirm_count")),
                # 실행정책(신규)
                "exec_partial": getattr(best_params, "exec_partial", "1.0"),
                "exec_time_stop_bars": int(getattr(best_params, "exec_time_stop_bars", 0)),
                "exec_trailing_mode": getattr(best_params, "exec_trailing_mode", "off"),
                "exec_trailing_k": float(getattr(best_params, "exec_trailing_k", 0.0)),
                "OPTIMIZED_METRIC": metric_name,
                "VALUE": float(round(metric_value, 4)) if not pd.isna(metric_value) else 0.0
            }
            with open(optimal_settings_file, 'w', encoding='utf-8') as f:
                json.dump(all_settings, f, indent=4, ensure_ascii=False)

            # (2) 전략 점수/지표 파라미터 저장
            base_strategies = config.get_strategy_configs(regime)
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
