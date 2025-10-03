# 파일명: analysis/indicator_calculator.py (데이터 소실 버그 해결을 위한 최종 버전)

import pandas as pd
import pandas_ta as ta

def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    [V5.1 최종] 데이터 전달 과정에서 발생하는 컬럼 소실 버그를 해결하기 위해
    가장 안정적인 데이터프레임 확장 방식을 사용하고, 최종 결과물을 출력하여 검증합니다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    # --- 1. 데이터 준비 및 정제 ---
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

    # --- 2. pandas-ta의 확장(extension) 기능을 사용하여 모든 지표를 순차적으로 추가 ---
    # 이 방식이 가장 안정적이며 데이터프레임 내부 구조를 해치지 않습니다.
    df_out.ta.ema(length=20, append=True)
    df_out.ta.ema(length=50, append=True)
    df_out.ta.ema(length=200, append=True)
    df_out.ta.rsi(append=True)
    df_out.ta.stoch(append=True)
    df_out.ta.obv(append=True)
    df_out.ta.mfi(append=True)
    df_out.ta.atr(append=True)
    df_out.ta.adx(append=True)
    df_out.ta.macd(append=True)
    
    # --- 3. 여러 컬럼을 반환하는 지표는 별도로 안전하게 처리 ---
    try:
        # 볼린저 밴드
        bbands_df = df_out.ta.bbands(length=20, std=2, append=False)
        if bbands_df is not None:
            df_out = pd.concat([df_out, bbands_df], axis=1)

        # 이치모쿠
        ichimoku_df, _ = df_out.ta.ichimoku(append=False)
        if ichimoku_df is not None:
            df_out = pd.concat([df_out, ichimoku_df], axis=1)
            if "ISA_9" in df_out.columns: df_out["ISA_9"] = df_out["ISA_9"].shift(-25)
            if "ISB_26" in df_out.columns: df_out["ISB_26"] = df_out["ISB_26"].shift(-25)

    except Exception as e:
        print(f"🚨 다중 컬럼 지표 병합 중 오류 발생: {e}")
    
    # --- 4. 최종 검증 단계 ---
    # 이 함수가 반환하기 직전의 최종 컬럼 목록을 출력하여 데이터 존재 여부를 증명합니다.
    print("--- [indicator_calculator] 최종 생성된 컬럼 목록 ---")
    print(df_out.columns.to_list())
    print("----------------------------------------------------")

    return df_out
