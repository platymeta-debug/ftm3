# analysis/indicator_calculator.py (호환성 문제 최종 해결)

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

    try:
        # --- ▼▼▼ [수정] 모든 버전에 호환되도록 개별 지표 직접 호출 방식으로 변경 ▼▼▼
        df_out.ta.ema(length=20, append=True)
        df_out.ta.ema(length=50, append=True)
        df_out.ta.ema(length=200, append=True)
        df_out.ta.rsi(append=True)
        df_out.ta.macd(append=True)
        df_out.ta.atr(append=True)
        df_out.ta.bbands(append=True)
        df_out.ta.adx(append=True)
        df_out.ta.ichimoku(append=True)
        df_out.ta.psar(append=True)
        df_out.ta.chop(append=True)
        df_out.ta.vortex(append=True)
        df_out.ta.trix(append=True)
        df_out.ta.stochrsi(append=True)
        df_out.ta.mfi(append=True)
        df_out.ta.cci(append=True)
        df_out.ta.ppo(append=True)
        df_out.ta.cmf(append=True)
        df_out.ta.efi(append=True)
        df_out.ta.kc(append=True)
        # --- ▲▲▲ [수정] ---

        # 이치모쿠 후행 지표 이동
        if "ISA_9" in df_out.columns and "ISB_26" in df_out.columns:
            df_out["ISA_9"] = df_out["ISA_9"].shift(-25)
            df_out["ISB_26"] = df_out["ISB_26"].shift(-25)

    except Exception as e:
        print(f"🚨 pandas-ta 지표 계산 중 심각한 오류 발생: {e}")

    print(f"--- [indicator_calculator] 총 {len(df_out.columns)}개의 컬럼(지표 포함) 생성 완료 ---")
    return df_out
