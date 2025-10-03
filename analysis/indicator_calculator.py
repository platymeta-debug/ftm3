# analysis/indicator_calculator.py (모든 지표 계산 최종 완성본)

import pandas as pd
import pandas_ta as ta

def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    try:
        df_out = df.copy()
        df_out.columns = [col.lower() for col in df_out.columns]
        core_cols = ['open', 'high', 'low', 'close', 'volume']
        for col in core_cols:
            df_out[col] = pd.to_numeric(df_out[col], errors='coerce')
        df_out.dropna(subset=core_cols, inplace=True)
        if df_out.empty: return pd.DataFrame()
    except Exception as e:
        print(f"🚨 데이터 준비 과정 오류: {e}")
        return pd.DataFrame()

    # ▼▼▼ [핵심 수정] ComprehensiveStrategy에서 사용하는 모든 지표를 추가합니다. ▼▼▼
    AllIndicatorsStrategy = ta.Strategy(
        name="Comprehensive Indicator Arsenal",
        description="Calculates a vast array of indicators for ML and analysis",
        ta=[
            # Trend (추세)
            {"kind": "ema", "length": 20},
            {"kind": "ema", "length": 50},
            {"kind": "ema", "length": 200},
            {"kind": "macd"},
            {"kind": "adx"},
            {"kind": "ichimoku"},
            {"kind": "psar"},
            {"kind": "chop"},
            {"kind": "vortex"},
            {"kind": "trix", "length": 30, "signal": 9}, # TRIX 추가

            # Momentum (모멘텀)
            {"kind": "rsi"},
            {"kind": "stoch"},
            {"kind": "stochrsi"}, # 스토캐스틱 RSI 추가
            {"kind": "mfi"},
            {"kind": "cci"},
            {"kind": "roc"},
            {"kind": "ppo"}, # PPO 추가
            {"kind": "cmo"},

            # Volume (거래량)
            {"kind": "obv"},
            {"kind": "vwap"},
            {"kind": "cmf"},
            {"kind": "efi"}, # 엘더의 힘 지수 추가

            # Volatility (변동성)
            {"kind": "bbands"},
            {"kind": "atr"},
            {"kind": "true_range"},
            {"kind": "donchian"},
            {"kind": "kc"}, # 켈트너 채널 추가
        ]
    )
    # ▲▲▲ [핵심 수정] ▲▲▲

    try:
        df_out.ta.strategy(AllIndicatorsStrategy)
    except Exception as e:
        print(f"🚨 pandas-ta 전략 실행 중 오류: {e}")

    # 이치모쿠 후행 지표 이동
    if "ISA_9" in df_out.columns and "ISB_26" in df_out.columns:
        df_out["ISA_9"] = df_out["ISA_9"].shift(-25)
        df_out["ISB_26"] = df_out["ISB_26"].shift(-25)

    print(f"--- [indicator_calculator] 총 {len(df_out.columns)}개의 컬럼(지표 포함) 생성 완료 ---")

    return df_out
