# -*- coding: utf-8 -*-
"""
local_backtesting/backtest_runner.py

- 여러 심볼 로컬 백테스트 러너
- optimal_settings.json + strategies_optimized.json 자동 로드
- 초기자본: TRADE_MODE(testnet/live) 및 API 키에 맞춰 Binance 잔고(견적자산)로 자동 설정
  * 현물(spot) 잔고 우선, 0이면 선물(USDⓈ-M) 잔고 사용
  * CLI --cash가 있으면 그 값이 최우선
- 기본 기간: 1년 (예: 4h ≈ 2190 캔들) — API limit 초과분은 자동 분할 조회(순/역방향 + Spot/Futures 폴백)
- 결과 저장: local_backtesting/results/<SYMBOL>/
- 콘솔 로그: 한국어 요약
"""

import os
import sys
import json
import time
import math
import argparse
import multiprocessing
from typing import Dict, Any, List, Optional, Callable

import numpy as np
import pandas as pd
from backtesting import backtesting
from backtesting.lib import FractionalBacktest
from binance.client import Client
from binance.exceptions import BinanceAPIException

# 프로젝트 루트 경로 추가
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# ==== 프로젝트 모듈 임포트 ====
from core.config_manager import config
from analysis import indicator_calculator
from local_backtesting.backtest_optimizer import OptoRunner  # 최적화 시 사용한 전략 재사용

# 멀티프로세싱
backtesting.Pool = multiprocessing.Pool

# === 결과 저장 루트 ===
RESULTS_ROOT = os.path.join(project_root, "local_backtesting", "results")


# ---------------- 유틸: JSON 직렬화 ----------------

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


# ---------------- 기간 → 캔들 수 ----------------

def _candles_per_day(timeframe: str) -> int:
    tf = timeframe.lower().strip()
    if tf.endswith("min"):
        mins = int(tf[:-3])
    elif tf.endswith("m"):
        mins = int(tf[:-1])
    elif tf.endswith("h"):
        mins = int(tf[:-1]) * 60
    elif tf.endswith("d"):
        mins = int(tf[:-1]) * 60 * 24
    else:
        mins = 240  # 기본 4h
    return max(1, (24 * 60) // mins)

def period_to_limit(period: str, timeframe: str) -> int:
    p = (period or "").strip().lower()
    if not p:
        p = "1y"
    if p.endswith('d'):
        days = int(p[:-1])
    elif p.endswith('m'):
        days = int(p[:-1]) * 30
    elif p.endswith('y'):
        days = int(p[:-1]) * 365
    else:
        try:
            return max(100, int(p))
        except Exception:
            days = 365
    return max(100, _candles_per_day(timeframe) * days)


# ---------------- 바이낸스 클라이언트/잔고 ----------------

def build_binance_client_from_env() -> Client:
    mode = (os.getenv("TRADE_MODE", "testnet") or "testnet").strip().lower()
    if mode in ("live", "mainnet", "real"):
        api_key = os.getenv("BINANCE_LIVE_API_KEY") or getattr(config, "api_key", "")
        api_secret = os.getenv("BINANCE_LIVE_API_SECRET") or getattr(config, "api_secret", "")
        client = Client(api_key, api_secret)
        return client
    else:
        api_key = os.getenv("BINANCE_TEST_API_KEY") or getattr(config, "api_key", "")
        api_secret = os.getenv("BINANCE_TEST_API_SECRET") or getattr(config, "api_secret", "")
        client = Client(api_key, api_secret)
        try:
            client.API_URL = 'https://testnet.binance.vision/api'
        except Exception:
            pass
        try:
            client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'
        except Exception:
            pass
        return client


def parse_quote_asset(symbol: str) -> str:
    symbol = symbol.upper()
    quotes = ["USDT", "BUSD", "USDC", "FDUSD", "TUSD", "BTC", "ETH", "BNB", "UST"]
    for q in quotes:
        if symbol.endswith(q):
            return q
    return "USDT"


def load_initial_cash_from_binance(client: Client, quote_asset: str) -> int:
    qa = quote_asset.upper()

    def _spot_balance(asset: str) -> float:
        try:
            bal = client.get_asset_balance(asset=asset)  # spot
            free = float(bal.get("free", 0) or 0)
            locked = float(bal.get("locked", 0) or 0)
            return free + locked
        except Exception:
            return 0.0

    def _futures_balance(asset: str) -> float:
        try:
            bals = client.futures_account_balance()  # USDⓈ-M
            for b in bals:
                if b.get("asset", "").upper() == asset:
                    return float(b.get("balance", 0) or 0)
        except Exception:
            pass
        return 0.0

    spot = _spot_balance(qa)
    if spot > 0:
        return int(math.floor(spot))

    futs = _futures_balance(qa)
    return int(math.floor(futs))


def load_initial_cash(cli_cash: Optional[int], client: Client, symbols: List[str]) -> int:
    if cli_cash is not None:
        return int(cli_cash)
    try:
        from collections import Counter
        quotes = [parse_quote_asset(s) for s in symbols] or ["USDT"]
        quote_asset = Counter(quotes).most_common(1)[0][0]
        cash = load_initial_cash_from_binance(client, quote_asset)
        if cash and cash > 0:
            return cash
    except Exception:
        pass
    return 10_000


# ---------------- Klines 분할 조회(순/역 + Spot/Futures) ----------------

def _interval_to_ms(interval: str) -> int:
    s = interval.strip().lower()
    if s.endswith("ms"):
        return int(s[:-2])
    if s.endswith("s"):
        return int(s[:-1]) * 1000
    if s.endswith("m"):
        return int(s[:-1]) * 60 * 1000
    if s.endswith("h"):
        return int(s[:-1]) * 60 * 60 * 1000
    if s.endswith("d"):
        return int(s[:-1]) * 24 * 60 * 60 * 1000
    return 4 * 60 * 60 * 1000  # 기본 4h


def _klines_to_df(raw: list) -> pd.DataFrame:
    """Binance klines list → DataFrame (Backtesting 표준 컬럼명, 인덱스 DateTime)"""
    if not raw:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    # Binance 포맷 고정 길이 가정
    # [ openTime, open, high, low, close, volume, closeTime, ... ]
    opens, highs, lows, closes, vols, times = [], [], [], [], [], []

    for row in raw:
        try:
            t = int(row[0])  # openTime (ms)
            o = float(row[1]) if row[1] not in (None, "", "null") else np.nan
            h = float(row[2]) if row[2] not in (None, "", "null") else np.nan
            l = float(row[3]) if row[3] not in (None, "", "null") else np.nan
            c = float(row[4]) if row[4] not in (None, "", "null") else np.nan
            v = float(row[5]) if row[5] not in (None, "", "null") else 0.0
        except Exception:
            # 한 줄이라도 이상하면 스킵
            continue
        times.append(pd.to_datetime(t, unit="ms"))
        opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)

    if not times:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    out = pd.DataFrame({
        "Open":  np.array(opens,  dtype="float64"),
        "High":  np.array(highs,  dtype="float64"),
        "Low":   np.array(lows,   dtype="float64"),
        "Close": np.array(closes, dtype="float64"),
        "Volume":np.array(vols,   dtype="float64"),
    }, index=pd.DatetimeIndex(times, name="Date"))

    # 안전장치: 너무 과거/미래값(±inf) 제거
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    return out


def _try_fetch_forward(call: Callable, symbol: str, interval: str, total_limit: int,
                       max_limit: int, cooldown: float, debug_tag: str) -> pd.DataFrame:
    interval_ms = _interval_to_ms(interval)
    results: List[list] = []
    try:
        end_now = int(call.__self__.get_server_time()["serverTime"])
    except Exception:
        end_now = int(time.time() * 1000)
    start_ts = end_now - (total_limit * interval_ms) - (5 * interval_ms)
    remain = total_limit
    safety = 0

    while remain > 0 and safety < 10000:
        safety += 1
        batch = min(max_limit, remain)
        try:
            raw = call(symbol=symbol, interval=interval, startTime=start_ts, limit=batch)
        except Exception:
            break
        if not raw:
            break

        results.extend(raw)
        last_open = raw[-1][0]
        start_ts = int(last_open) + interval_ms
        remain -= len(raw)
        print(f"   ↗ {debug_tag}: {len(raw)}개 적재 (누적 {len(results)} / 목표 {total_limit})")
        time.sleep(cooldown)
        if len(raw) < 1:
            break

    return _klines_to_df(results)


def _try_fetch_backward(call: Callable, symbol: str, interval: str, total_limit: int,
                        max_limit: int, cooldown: float, debug_tag: str) -> pd.DataFrame:
    interval_ms = _interval_to_ms(interval)
    chunks: List[pd.DataFrame] = []
    remain = total_limit
    safety = 0
    try:
        end_now = int(call.__self__.get_server_time()["serverTime"])
    except Exception:
        end_now = int(time.time() * 1000)
    end_ts = end_now

    while remain > 0 and safety < 10000:
        safety += 1
        batch = min(max_limit, remain)
        try:
            raw = call(symbol=symbol, interval=interval, endTime=end_ts, limit=batch)
        except Exception:
            break
        if not raw:
            break

        df = _klines_to_df(raw)
        if df.empty:
            break
        chunks.append(df)
        remain -= len(df)
        print(f"   ↘ {debug_tag}: {len(df)}개 적재 (누적 {total_limit - remain} / 목표 {total_limit})")

        first_open = int(df.index[0].value // 10**6)  # ms
        end_ts = first_open - interval_ms
        time.sleep(cooldown)

        if len(df) < 1:
            break

    if not chunks:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    out = pd.concat(chunks).sort_index()
    if len(out) > total_limit:
        out = out.iloc[-total_limit:]
    return out


def fetch_klines_resilient(client: Client, symbol: str, interval: str, total_limit: int,
                           prefer: str = "spot", cooldown: float = 0.2) -> pd.DataFrame:
    MAX_SPOT = 1000
    MAX_FUTS = 1500

    def _spot(**kw):
        return client.get_klines(**kw)

    def _futs(**kw):
        return client.futures_klines(**kw)

    if prefer == "spot":
        first_call, second_call = _spot, _futs
        max_first, max_second = MAX_SPOT, MAX_FUTS
    else:
        first_call, second_call = _futs, _spot
        max_first, max_second = MAX_FUTS, MAX_SPOT

    df = _try_fetch_forward(first_call, symbol, interval, total_limit, max_first, cooldown, f"{symbol}/first-forward")
    if len(df) >= total_limit // 2:
        return df if len(df) >= total_limit else df

    df_back = _try_fetch_backward(first_call, symbol, interval, total_limit, max_first, cooldown, f"{symbol}/first-backward")
    if len(df_back) >= total_limit:
        return df_back

    df2 = _try_fetch_forward(second_call, symbol, interval, total_limit, max_second, cooldown, f"{symbol}/second-forward")
    if len(df2) >= total_limit:
        return df2

    df2_back = _try_fetch_backward(second_call, symbol, interval, total_limit, max_second, cooldown, f"{symbol}/second-backward")
    return df2_back


# ---------------- 파라미터 로드 ----------------

def load_exec_params(opt_json_path: str, symbol: str, regime: str) -> Dict[str, Any]:
    defaults = {
        "open_threshold": 12,
        "risk_reward_ratio": 2.0,
        "sl_atr_multiplier": 1.5,
        "trend_entry_confirm_count": 3,
    }
    try:
        with open(opt_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(regime, {}).get(symbol, {})
    except Exception:
        entry = {}

    params = dict(defaults)
    if entry:
        if "OPEN_TH" in entry:
            params["open_threshold"] = int(entry["OPEN_TH"])
        if "RR_RATIO" in entry:
            params["risk_reward_ratio"] = float(entry["RR_RATIO"])
        if "SL_ATR_MULTIPLIER" in entry:
            params["sl_atr_multiplier"] = float(entry["SL_ATR_MULTIPLIER"])
        if "TREND_ENTRY_CONFIRM_COUNT" in entry:
            params["trend_entry_confirm_count"] = int(entry["TREND_ENTRY_CONFIRM_COUNT"])
    return params


def load_strategy_params(strat_json_path: str, regime: str) -> Dict[str, Any]:
    params = {
        "ema_short": 20,
        "ema_long": 50,
        "score_strong_trend": 5,
        "rsi_period": 14,
        "rsi_oversold": 30,
        "score_oversold": 5,
        "score_macd_cross_up": 2,
        "adx_threshold": 25,
        "score_adx_strong": 3,
    }
    try:
        with open(strat_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        reg = data.get(regime, {})
        trend = (reg.get("TrendStrategy") or {})
        osci = (reg.get("OscillatorStrategy") or {})
        comp = (reg.get("ComprehensiveStrategy") or {})

        if "ema_short" in trend: params["ema_short"] = int(trend["ema_short"])
        if "ema_long" in trend: params["ema_long"] = int(trend["ema_long"])
        if "score_strong_trend" in trend: params["score_strong_trend"] = int(trend["score_strong_trend"])

        if "rsi_period" in osci: params["rsi_period"] = int(osci["rsi_period"])
        if "rsi_oversold" in osci: params["rsi_oversold"] = int(osci["rsi_oversold"])
        if "score_oversold" in osci: params["score_oversold"] = int(osci["score_oversold"])

        if "score_macd_cross_up" in comp: params["score_macd_cross_up"] = int(comp["score_macd_cross_up"])
        if "adx_threshold" in comp: params["adx_threshold"] = int(comp["adx_threshold"])
        if "score_adx_strong" in comp: params["score_adx_strong"] = int(comp["score_adx_strong"])

    except Exception:
        pass

    if params["ema_short"] >= params["ema_long"]:
        params["ema_short"], params["ema_long"] = 20, 50

    return params


def to_bt_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    need_cols = {"Open","High","Low","Close","Volume"}
    if set(df.columns) >= need_cols:
        out = df.copy()
        out.index.name = "Date"
        return out
    out = df.copy()
    out.columns = [c.capitalize() for c in out.columns]
    out.index.name = "Date"
    return out


# ---------------- 데이터 클린업(핵심 추가) ----------------

def clean_ohlc_df(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """
    - 인덱스 정렬, 중복 타임스탬프 제거
    - OHLC 결측 수리: Close 값으로 보정(가능할 때만)
    - 그래도 남는 결측은 드롭
    - 무한대값 제거
    """
    if df is None or df.empty:
        return df.copy()

    df = df.copy()
    # 정렬 + 중복 제거
    df = df[~df.index.duplicated(keep="first")].sort_index()

    # 타입 보장(여기선 이미 float64지만 방어)
    for c in ("Open", "High", "Low", "Close", "Volume"):
        if c in df.columns:
            df[c] = df[c].astype("float64")

    # 무한대 제거 → NaN
    df.replace([np.inf, -np.inf], np.nan, inplace=True)

    # —— 수리 단계: OHLC 중 NaN을 Close로 메꿈(가능할 때만)
    ohlc = ["Open", "High", "Low", "Close"]
    before_nan = df[ohlc].isna().sum().sum()

    # Close가 있는 곳에서만 다른 칼럼 보전
    for col in ("Open", "High", "Low"):
        need = df[col].isna() & df["Close"].notna()
        df.loc[need, col] = df.loc[need, "Close"]

    # Close 자체가 NaN이면, 직전/직후 값으로 보간 시도(경미한 구멍 메움)
    if df["Close"].isna().any():
        df["Close"] = df["Close"].interpolate(limit_direction="both")

    # 보간 후에도 남은 NaN은 드롭 (OHLC 중 하나라도 NaN이면 제거)
    mask_finite = np.isfinite(df[ohlc]).all(axis=1)
    repaired_nan = before_nan - df[ohlc].isna().sum().sum()
    dropped = int((~mask_finite).sum())
    if repaired_nan > 0:
        print(f"   🔧 OHLC 결측 {int(repaired_nan)}개 수리")
    if dropped > 0:
        print(f"   🧹 수리 불가 행 {dropped}개 제거")

    df = df[mask_finite]

    return df



# ---------------- 출력/저장 ----------------

def _fmt(v, digits=4):
    try:
        if v is None:
            return "-"
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _collect_summary(stats: pd.Series, initial_cash: int) -> Dict[str, Any]:
    s = stats
    return {
        "초기자본[$]": initial_cash,
        "최종자본[$]": s.get("Equity Final [$]"),
        "수익률[%]": s.get("Return [%]"),
        "연환산수익률[%]": s.get("Return (Ann.) [%]", s.get("CAGR [%]")),
        "최대낙폭[%]": s.get("Max. Drawdown [%]"),
        "거래수": s.get("# Trades"),
        "승률[%]": s.get("Win Rate [%]"),
        "프로핏팩터": s.get("Profit Factor"),
        "평균거래수익[%]": s.get("Avg. Trade [%]", s.get("Average Trade [%]")),
        "샤프비율": s.get("Sharpe Ratio"),
        "칼마비율": s.get("Calmar Ratio"),
        "SQN": s.get("SQN"),
    }


def save_stats(symbol: str, regime: str, stats: pd.Series, params: Dict[str, Any], results_root: str):
    out_dir = os.path.join(results_root, symbol)
    os.makedirs(out_dir, exist_ok=True)

    scalars = {}
    for k, v in stats.items():
        try:
            is_scalar = pd.api.types.is_scalar(v) or isinstance(v, (int, float, str, bool, type(None), pd.Timestamp))
        except Exception:
            is_scalar = isinstance(v, (int, float, str, bool, type(None), pd.Timestamp))
        if not is_scalar:
            continue
        try:
            if pd.isna(v):
                v = None
        except Exception:
            pass
        if isinstance(v, pd.Timestamp):
            v = v.isoformat()
        scalars[k] = _to_jsonable_dict({"_": v})["_"]

    with open(os.path.join(out_dir, f"{symbol}_{regime}_지표.json"), "w", encoding="utf-8") as f:
        json.dump(scalars, f, indent=2, ensure_ascii=False)

    trades = getattr(stats, "_trades", None)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(os.path.join(out_dir, f"{symbol}_{regime}_트레이드.csv"), index=False)

    with open(os.path.join(out_dir, f"{symbol}_{regime}_파라미터.json"), "w", encoding="utf-8") as f:
        json.dump(_to_jsonable_dict(params), f, indent=2, ensure_ascii=False)


# ---------------- 실행 ----------------

def run_once(client: Client, symbol: str, regime: str, timeframe: str, limit: int, cash: int, report_html: bool = True):
    print(f"\n🚀 [{symbol}] 백테스트 시작… (시장국면={regime}, 주기={timeframe}, 봉수={limit})")

    # 1) 데이터 수집(탄탄한 분할 조회)
    df_raw = fetch_klines_resilient(client, symbol, timeframe, total_limit=limit, prefer="spot")
    print(f"   ⛏ 수집된 캔들 수: {len(df_raw)} / 목표 {limit}")
    if df_raw is None or len(df_raw) < 200:
        raise RuntimeError(f"[건너뜀] {symbol} 데이터 부족: {len(df_raw) if df_raw is not None else 0}")

    # 2) 데이터 클린업(핵심)
    df_raw = clean_ohlc_df(df_raw, timeframe)
    if len(df_raw) < 200:
        raise RuntimeError(f"[건너뜀] {symbol} 클린업 후 데이터 부족: {len(df_raw)}")

    # 3) 지표 생성
    _ = indicator_calculator.calculate_all_indicators(df_raw)

    # 4) 전략 컨텍스트
    OptoRunner.symbol = symbol
    OptoRunner.market_regime = regime

    # 5) Backtesting 포맷
    df_bt = to_bt_dataframe(df_raw)

    # 6) 파라미터 로드
    optimal_settings_file = os.path.join(project_root, "optimal_settings.json")
    strategies_optimized_file = os.path.join(project_root, "strategies_optimized.json")
    exec_params = load_exec_params(optimal_settings_file, symbol, regime)
    strat_params = load_strategy_params(strategies_optimized_file, regime)
    params = {**exec_params, **strat_params}

    # 7) 백테스트
    os.makedirs(RESULTS_ROOT, exist_ok=True)
    bt = FractionalBacktest(
        df_bt,
        OptoRunner,
        cash=cash,
        commission=.002,
        margin=1/10,
        finalize_trades=True,
    )
    stats = bt.run(**params)

    # 8) 요약/출력
    summary = _collect_summary(stats, cash)
    print("—" * 70)
    print(f"📈 [{symbol}] 백테스트 결과 요약")
    print(f"   초기자본[$]     : {summary['초기자본[$]']:,}")
    if summary['최종자본[$]'] is not None:
        print(f"   최종자본[$]     : {int(summary['최종자본[$]']):,}")
    else:
        print(f"   최종자본[$]     : -")
    print(f"   수익률[%]       : {_fmt(summary['수익률[%]'])}")
    print(f"   연환산수익률[%] : {_fmt(summary['연환산수익률[%]'])}")
    print(f"   최대낙폭[%]     : {_fmt(summary['최대낙폭[%]'])}")
    print(f"   거래수          : {_fmt(summary['거래수'], 0)}")
    print(f"   승률[%]         : {_fmt(summary['승률[%]'])}")
    print(f"   프로핏팩터      : {_fmt(summary['프로핏팩터'])}")
    print(f"   샤프비율        : {_fmt(summary['샤프비율'])}")
    print(f"   칼마비율        : {_fmt(summary['칼마비율'])}")
    print(f"   평균거래수익[%] : {_fmt(summary['평균거래수익[%]'])}")
    print("—" * 70)
    print("   적용 파라미터   :", _to_jsonable_dict(params))

    # 9) 저장
    out_dir = os.path.join(RESULTS_ROOT, symbol)
    os.makedirs(out_dir, exist_ok=True)

    if report_html:
        out_path = os.path.join(out_dir, f"{symbol}_{regime}_리포트.html")
        try:
            bt.plot(open_browser=False, filename=out_path)
            print(f"🧾 HTML 리포트 저장 완료 → {out_path}")
        except Exception as e:
            print(f"[경고] HTML 리포트 생성 실패: {e}")

    save_stats(symbol, regime, stats, params, RESULTS_ROOT)

    summary_txt = os.path.join(out_dir, f"{symbol}_{regime}_요약.txt")
    with open(summary_txt, "w", encoding="utf-8") as f:
        f.write(f"[{symbol}] 백테스트 요약\n")
        f.write("-" * 50 + "\n")
        for k, v in summary.items():
            f.write(f"{k} : {v}\n")
        f.write("-" * 50 + "\n")
        f.write("파라미터:\n")
        f.write(json.dumps(_to_jsonable_dict(params), ensure_ascii=False, indent=2))
    print(f"💾 결과 저장 완료 → {out_dir}")

    return stats


def parse_symbols(single_symbol: Optional[str], symbols_csv: Optional[str]) -> List[str]:
    if symbols_csv:
        return [s.strip() for s in symbols_csv.split(",") if s.strip()]
    if single_symbol:
        return [single_symbol.strip()]
    return ["BTCUSDT", "ETHUSDT"]


def main():
    parser = argparse.ArgumentParser(description="Local backtest runner (multi-symbol)")
    parser.add_argument("--symbol", help="단일 심볼 (예: BTCUSDT)")
    parser.add_argument("--symbols", help="복수 심볼 CSV (예: BTCUSDT,ETHUSDT)")
    parser.add_argument("--regime", default=os.getenv("RUN_REGIME", "BULL"), choices=["BULL", "BEAR", "SIDEWAYS"])
    parser.add_argument("--tf", default=os.getenv("RUN_TIMEFRAME", "4h"))
    parser.add_argument("--limit", type=int, default=None, help="캔들 수 직접 지정")
    parser.add_argument("--period", default="1y", help="기간 지정(예: 6m, 1y, 180d). 지정 시 --limit보다 우선")
    parser.add_argument("--cash", type=int, default=None)
    parser.add_argument("--no-report", dest="no_report", action="store_true")
    args = parser.parse_args()

    symbols = parse_symbols(args.symbol, args.symbols)
    client = build_binance_client_from_env()
    initial_cash = load_initial_cash(args.cash, client, symbols)
    print(f"💰 초기자본 설정: {initial_cash:,}  (원천: CLI > Binance잔고 > 기본)")

    if args.period:
        effective_limit = period_to_limit(args.period, args.tf)
        print(f"🗓️ 기간 기준 백테스트: period={args.period} → limit={effective_limit} (tf={args.tf})")
    elif args.limit:
        effective_limit = int(args.limit)
        print(f"📏 캔들 수 기준 백테스트: limit={effective_limit} (tf={args.tf})")
    else:
        effective_limit = period_to_limit("1y", args.tf)
        print(f"🗓️ 기본 기간(1y) 백테스트: limit={effective_limit} (tf={args.tf})")

    try:
        for sym in symbols:
            run_once(
                client=client,
                symbol=sym,
                regime=args.regime,
                timeframe=args.tf,
                limit=effective_limit,
                cash=initial_cash,
                report_html=(not args.no_report),
            )
    except Exception as e:
        print(f"[오류] 백테스트 실패: {e}")
        raise


if __name__ == "__main__":
    main()
