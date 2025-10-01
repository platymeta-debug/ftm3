# 파일명: analysis/indicator_calculator.py (전체 최종 수정안)

import pandas as pd
import pandas_ta as ta

def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    주어진 DataFrame에 설계서에 명시된 모든 기술적 지표를 계산하여 추가합니다.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    # 각 지표를 개별적으로 계산하고 DataFrame에 추가합니다.
    # 이 방식이 가장 안정적입니다.
    df.ta.ichimoku(append=True)
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.ema(length=200, append=True)
    df.ta.rsi(append=True)
    df.ta.macd(append=True)
    df.ta.bbands(append=True)
    df.ta.atr(append=True)
    df.ta.adx(append=True)

    # 모든 컬럼을 숫자로 변환, 변환 불가 시 NaN으로 처리하여 오류 방지
    for col in df.columns:
        if col not in ['open', 'high', 'low', 'close', 'volume']:
             df[col] = pd.to_numeric(df[col], errors='coerce')

    return df
