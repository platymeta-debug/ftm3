import pandas as pd
import pandas_ta as ta


def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """주어진 DataFrame에 설계서에 명시된 모든 기술적 지표를 계산하여 추가합니다."""
    if df is None or df.empty:
        return pd.DataFrame()

    # pandas-ta 전략을 사용하여 모든 지표를 한번에 계산 (더 효율적)
    custom_strategy = ta.Strategy(
        name="Confluence Strategy",
        description="All indicators for the confluence engine",
        ta=[
            {"kind": "ichimoku"},
            {"kind": "ema", "length": 20},
            {"kind": "ema", "length": 50},
            {"kind": "ema", "length": 200},
            {"kind": "rsi"},
            {"kind": "macd"},
            {"kind": "bbands"},
            {"kind": "atr"},
            {"kind": "adx"},
        ]
    )

    df.ta.strategy(custom_strategy)

    # 모든 컬럼을 숫자로 변환, 변환 불가 시 NaN으로 처리하여 오류 방지
    for col in df.columns:
        if col not in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    return df
