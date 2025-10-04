# -*- coding: utf-8 -*-
# local_backtesting/backtest_optimizer.py
import multiprocessing
from backtesting import backtesting
backtesting.Pool = multiprocessing.Pool

import pandas as pd
import json
from backtesting import Strategy
from backtesting.lib import FractionalBacktest
from binance.client import Client
from collections import deque
import sys
import os
from tqdm import tqdm

# numpy/pandas 값들을 파이썬 내장형으로 캐스팅해 JSON 직렬화 가능하게 변환
def _to_jsonable_dict(d: dict) -> dict:
    def conv(x):
        # numpy 계열
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
        # pandas 계열
        if isinstance(x, pd.Timestamp):
            return x.isoformat()
        # 기본형은 그대로
        if isinstance(x, (int, float, bool, str)) or x is None:
            return x
        # 그 외는 문자열로 안전 변환
        try:
            return float(x)
        except Exception:
            try:
                return int(x)
            except Exception:
                return str(x)
    return {k: conv(v) for k, v in d.items()}


# --- 프로젝트 경로 설정 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

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
    # 1) 실행 조건
    open_threshold = 12.0
    risk_reward_ratio = 2.0
    sl_atr_multiplier = 1.5
    trend_entry_confirm_count = 3
    # 2) Trend
    ema_short = 20
    ema_long = 50
    score_strong_trend = 5
    # 3) Oscillator(요지)
    rsi_period = 14
    rsi_oversold = 30
    rsi_overbought = 70
    score_oversold = 5
    score_overbought = -5
    # 4) Comprehensive(요지)
    score_macd_cross_up = 2
    adx_threshold = 25
    score_adx_strong = 3
    score_bb_breakout_up = 4
    score_chop_trending = 3

    # (내부)
    symbol = "BTCUSDT"
    market_regime = "BULL"

    def init(self):
        # backtesting 파라미터 → 전략 Config 구성
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
                # 나머지는 기본값(최적화 속도)
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
                # 나머지는 기본값
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
        self.indicators = indicator_calculator.calculate_all_indicators(self.data.df)
        self.recent_scores = deque(maxlen=int(self.trend_entry_confirm_count))

    def next(self):
        idx = len(self.data) - 1
        cur = self.indicators.iloc[:idx + 1]
        if len(cur) < self.trend_entry_confirm_count:
            return

        current_score, _ = self.engine._calculate_tactical_score(cur)
        self.recent_scores.append(current_score)
        if len(self.recent_scores) < self.trend_entry_confirm_count:
            return

        avg_score = sum(self.recent_scores) / len(self.recent_scores)
        side = None
        if self.market_regime == "BULL" and avg_score >= self.open_threshold:
            side = "BUY"
        elif self.market_regime == "BEAR" and avg_score <= -self.open_threshold:
            side = "SELL"

        if side and not self.position:
            atr = cur.iloc[-1].get("ATRr_14", 0)
            if not atr or pd.isna(atr) or atr <= 0:
                return
            sl_d = atr * self.sl_atr_multiplier
            tp_d = sl_d * self.risk_reward_ratio
            price = self.data.Close[-1]
            sl = price - sl_d if side == "BUY" else price + sl_d
            tp = price + tp_d if side == "BUY" else price - tp_d
            if sl <= 0 or tp <= 0:
                return
            if side == "BUY":
                self.buy(sl=sl, tp=tp, size=0.5)
            else:
                self.sell(sl=sl, tp=tp, size=0.5)


# 결과 요약에 표시할 파라미터 키(중복 사용 방지)
BEST_PARAM_KEYS = [
    "open_threshold","risk_reward_ratio","sl_atr_multiplier","trend_entry_confirm_count",
    "ema_short","ema_long","score_strong_trend",
    "rsi_period","rsi_oversold","score_oversold",
    "score_macd_cross_up","adx_threshold","score_adx_strong",
]


# ---- 공통 유틸: 파라미터→백테스트 실행(최적화기 공용) ----
def run_backtest_with_params(
    df_capitalized: pd.DataFrame,
    params: dict,
    initial_cash: int,
    symbol: str,
    regime: str
):
    """
    공통 목표함수용 백테스트 러너.
    - 선호 지표: Calmar → Sharpe → Return
    - 과대평가 방지용 가드:
        * 최소 트레이드 수 미만이면 점수 대폭 감점
        * MDD 분모 과소(≈0)에 따른 Calmar 폭주 시 Sharpe/Return으로 폴백
    - 환경변수(.env)로 튜닝 가능:
        OPT_MIN_TRADES=50
        OPT_MDD_FLOOR_PCT=3.0
    """
    import os, math

    # 전략 컨텍스트 주입
    OptoRunner.symbol = symbol
    OptoRunner.market_regime = regime

    # ✅ finalize_trades 는 Backtest 생성자 인자
    bt = FractionalBacktest(
        df_capitalized,
        OptoRunner,
        cash=initial_cash,
        commission=.002,
        margin=1 / 10,
        finalize_trades=True,   # ← 여기!
    )
    stats = bt.run(**params)    # ← run()에는 넣지 않음

    # ---- 안정화 가드 파라미터 ----
    min_trades = int(os.getenv("OPT_MIN_TRADES", 50))
    mdd_floor = float(os.getenv("OPT_MDD_FLOOR_PCT", 3.0))  # [%] 기준

    # ---- 숫자 파싱 유틸 ----
    def _f(x, default=float("nan")):
        try:
            v = float(x)
            return v
        except Exception:
            return default

    def _finite(x):
        return (x is not None) and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))

    # ---- 핵심 지표 추출 ----
    trades = int(stats.get("# Trades", 0) or 0)
    mdd = abs(_f(stats.get("Max. Drawdown [%]", 0), 0.0))

    calmar = _f(stats.get("Calmar Ratio"))
    sharpe = _f(stats.get("Sharpe Ratio"))
    retpct = _f(stats.get("Return [%]"), 0.0)

    # ---- 가드 1: 트레이드 수 부족 시 강한 감점 ----
    if trades < min_trades:
        return stats, -1e12, f"Rejected: few trades (<{min_trades})"

    # ---- 가드 2: MDD 분모 과소시 Calmar 왜곡 방지 → 폴백 ----
    if mdd < mdd_floor:
        if _finite(sharpe):
            return stats, float(sharpe), "Sharpe Ratio (fallback)"
        return stats, float(retpct), "Return [%] (fallback)"

    # ---- 기본 선호: Calmar → Sharpe → Return ----
    if _finite(calmar):
        return stats, float(calmar), "Calmar Ratio"
    if _finite(sharpe):
        return stats, float(sharpe), "Sharpe Ratio"
    return stats, float(retpct), "Return [%]"


def get_param_spaces():
    """
    탐색공간(그리드/GA/베이지안 공통).
    int/float/cat 타입 혼용 지원.
    """
    return {
        "open_threshold":       {"type":"int",   "low": 8,   "high": 22, "choices":[10,12,14,16]},
        "risk_reward_ratio":    {"type":"float", "low": 1.4, "high": 3.8, "choices":[1.8,2.0,2.5,3.0]},
        "sl_atr_multiplier":    {"type":"float", "low": 1.0, "high": 3.0, "choices":[1.2,1.5,1.8,2.2]},
        "trend_entry_confirm_count":{"type":"int","low": 1,   "high": 5,  "choices":[2,3,4]},
        "ema_short":            {"type":"int",   "low": 8,   "high": 28, "choices":[12,16,20,24]},
        "ema_long":             {"type":"int",   "low": 34,  "high":120, "choices":[40,50,60,80]},
        "score_strong_trend":   {"type":"int",   "low": 2,   "high": 6,  "choices":[3,4,5]},
        "rsi_period":           {"type":"int",   "low": 10,  "high": 20, "choices":[14]},
        "rsi_oversold":         {"type":"int",   "low": 18,  "high": 35, "choices":[20,25,30]},
        "score_oversold":       {"type":"int",   "low": 2,   "high": 6,  "choices":[3,4,5]},
        "score_macd_cross_up":  {"type":"int",   "low": 1,   "high": 5,  "choices":[2,3,4]},
        "adx_threshold":        {"type":"int",   "low": 15,  "high": 35, "choices":[18,22,25,28]},
        "score_adx_strong":     {"type":"int",   "low": 1,   "high": 5,  "choices":[2,3,4]},
    }


def grid_choice_count(param_spaces):
    # choices가 있는 것만 카운트하여 grid 경우의 수 추정
    total = 1
    for s in param_spaces.values():
        ch = s.get("choices")
        if ch:
            total *= len(ch)
    return total


def choose_method_auto(param_spaces):
    # 환경변수로 강제 지정 가능
    env = os.getenv("OPT_METHOD", "auto").lower()
    if env in ("grid", "ga", "bayes"):
        return env

    # 자동 판정
    combos = grid_choice_count(param_spaces)  # 대략적 그리드 조합 수
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
    binance_client = Client(config.api_key, config.api_secret)

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

        for regime in ["BULL", "BEAR"]:
            print(f"\n--- 🔬 [{symbol}] '{regime}' 구간 최적화 ---")
            df = segmented.get(regime)
            if df is None or len(df) < 100:
                print(f"[SKIP] '{regime}' 구간 데이터 부족")
                continue

            # Backtesting 표준 컬럼명
            df = df.copy()
            df.columns = [c.capitalize() for c in df.columns]

            # --- 방법 분기 ---
            if method == "grid":
                # Backtest 생성 시 finalize_trades 활성화
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
                    maximize='Calmar Ratio',
                    constraint=lambda p: p.ema_short < p.ema_long and p.risk_reward_ratio > p.sl_atr_multiplier
                )
                best_params = stats._strategy
                metric_name = 'Calmar Ratio'
                metric_value = float(stats[metric_name]) if metric_name in stats and pd.notna(stats[metric_name]) else 0.0

            elif method in ("ga", "bayes") and _HAS_OPTIMIZERS:
                # 공통 objective
                def objective(eval_params: dict) -> float:
                    snapped = {}
                    for k, s in param_spaces.items():
                        v = eval_params.get(k)
                        if s.get("choices"):
                            ch = s["choices"]
                            v = min(ch, key=lambda z: abs(z - v)) if isinstance(ch[0], (int, float)) else (v if v in ch else ch[0])
                        snapped[k] = v
                    # 제약
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
                best_params = best_params_obj  # 통일
                best_kv = {k: getattr(best_params, k) for k in BEST_PARAM_KEYS if hasattr(best_params, k)}

                # 리포트용 재실행 + HTML 저장 (생성자에 finalize_trades)
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

                metric_name = "Calmar Ratio"  # 표시상 통일

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
            # (1) 실행 파라미터 저장
            if regime not in all_settings:
                all_settings[regime] = {}
            all_settings[regime][symbol] = {
                "OPEN_TH": int(getattr(best_params, "open_threshold")),
                "RR_RATIO": float(getattr(best_params, "risk_reward_ratio")),
                "SL_ATR_MULTIPLIER": float(getattr(best_params, "sl_atr_multiplier")),
                "TREND_ENTRY_CONFIRM_COUNT": int(getattr(best_params, "trend_entry_confirm_count")),
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
