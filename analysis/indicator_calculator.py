# analysis/indicator_calculator.py (모든 지표 계산 최종본)

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

    # ▼▼▼ [핵심] pandas-ta 라이브러리의 모든 주요 지표를 포함하는 전략 생성 ▼▼▼
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
            {"kind": "vortex"}, # Vortex Indicator

            # Momentum (모멘텀)
            {"kind": "rsi"},
            {"kind": "stoch"},
            {"kind": "stochrsi"},
            {"kind": "mfi"},
            {"kind": "cci"},
            {"kind": "roc"},
            {"kind": "ppo"}, # Percentage Price Oscillator
            {"kind": "trix"}, # Trix
            {"kind": "cmo"}, # Chande Momentum Oscillator

            # Volume (거래량)
            {"kind": "obv"},
            {"kind": "vwap"},
            {"kind": "cmf"}, # Chaikin Money Flow
            {"kind": "efi"}, # Elder's Force Index

            # Volatility (변동성)
            {"kind": "bbands"},
            {"kind": "atr"},
            {"kind": "true_range"},
            {"kind": "donchian"}, # Donchian Channels
            {"kind": "kc"}, # Keltner Channels

            # Other (기타/사용자 정의)
            # 예시: 특정 기간의 최고/최저가
            {"kind": "highest", "length": 50},
            {"kind": "lowest", "length": 50},
        ]
    )
    # ▲▲▲ [핵심] ▲▲▲

    try:
        df_out.ta.strategy(AllIndicatorsStrategy)
    except Exception as e:
        print(f"🚨 pandas-ta 전략 실행 중 오류: {e}")

    # 이치모쿠 후행 지표 이동
    if "ISA_9" in df_out.columns and "ISB_26" in df_out.columns:
        df_out["ISA_9"] = df_out["ISA_9"].shift(-25)
        df_out["ISB_26"] = df_out["ISB_26"].shift(-25)

    print(f"--- [indicator_calculator] 총 {len(df_out.columns)}개의 컬럼(지표 포함) 생성 완료 ---")
    # print(df_out.columns.to_list()) # 너무 길어서 주석 처리

    return df_out
