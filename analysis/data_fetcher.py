from binance.client import Client
import pandas as pd


def fetch_klines(client: Client, symbol: str, timeframe: str, limit: int = 1000) -> pd.DataFrame | None:  # limit 기본값 변경
    """
    바이낸스에서 K-line(캔들) 데이터를 가져와 pandas DataFrame으로 변환합니다.
    """
    try:
        # testnet 모드 여부와 상관없이, 분석 데이터는 항상 실서버(fapi)에서 가져옵니다.
        # python-binance 라이브러리는 Client 초기화 시 testnet=True여도 klines 조회는 실서버를 바라봅니다.
        klines = client.futures_klines(symbol=symbol, interval=timeframe, limit=limit)
        if not klines:
            return None

        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])

        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])

        return df[['open', 'high', 'low', 'close', 'volume']]

    except Exception as e:
        print(f"Error fetching klines for {symbol} ({timeframe}): {e}")
        return None
