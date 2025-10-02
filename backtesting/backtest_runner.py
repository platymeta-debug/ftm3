# backtesting/backtest_runner.py (V2 - 멀티 심볼 지원)

import pandas as pd
from backtesting import Backtest, Strategy
from binance.client import Client
import sys
import os

# 프로젝트의 루트 디렉토리를 경로에 추가하여 다른 모듈들을 불러올 수 있게 합니다.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.confluence_engine import ConfluenceEngine
from analysis.data_fetcher import fetch_klines
from core.config_manager import config  # 설정 파일에서 SYMBOLS 리스트를 가져오기 위해 import

# 1. Backtesting.py를 위한 데이터 컬럼명 조정 (기존과 동일)
def prepare_data_for_backtesting(df: pd.DataFrame) -> pd.DataFrame:
    """Backtesting.py에 맞게 데이터프레임 컬럼명을 변경합니다."""
    df_renamed = df.rename(columns={
        'open': 'Open',
        'high': 'High',
        'low': 'Low',
        'close': 'Close',
        'volume': 'Volume'
    })
    return df_renamed

# 2. Backtesting.py의 Strategy 클래스 (기존과 동일)
class ConfluenceStrategy(Strategy):
    """기존 ConfluenceEngine의 로직을 백테스팅용 전략으로 변환합니다."""
    
    def init(self):
        mock_client = Client("", "")
        self.confluence_engine = ConfluenceEngine(mock_client)
        print("백테스팅을 위한 ConfluenceStrategy 초기화 완료.")

    def next(self):
        df = pd.DataFrame({
            'open': self.data.Open, 'high': self.data.High,
            'low': self.data.Low, 'close': self.data.Close,
            'volume': self.data.Volume
        })

        if len(df) < 200:
            return

        # 임시 분석 로직: 간단한 이동평균선 교차 전략
        sma5 = pd.Series(self.data.Close).rolling(5).mean().iloc[-1]
        sma20 = pd.Series(self.data.Close).rolling(20).mean().iloc[-1]
        
        if sma5 > sma20 and not self.position:
            self.buy()
        elif sma5 < sma20 and self.position:
            self.position.close()

# --- ▼▼▼ [핵심 수정] 백테스팅 실행 스크립트 ▼▼▼ ---

# 3. 백테스팅 실행 스크립트
if __name__ == '__main__':
    # 바이낸스 클라이언트 초기화
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)

    # config.py에 정의된 모든 심볼에 대해 백테스팅을 반복 실행
    for symbol in config.symbols:
        print(f"\n{'='*50}")
        print(f"🚀 {symbol}에 대한 백테스팅을 시작합니다...")
        print(f"{'='*50}")
        
        # 과거 데이터 가져오기 (1일봉, 500개)
        print(f"바이낸스에서 {symbol}의 과거 데이터를 다운로드합니다...")
        klines_data = fetch_klines(binance_client, symbol, "1d", limit=500)

        if klines_data is not None and not klines_data.empty:
            print("데이터 다운로드 완료. 백테스팅을 준비합니다.")
            data_for_bt = prepare_data_for_backtesting(klines_data)

            # 백테스트 객체 생성
            bt = Backtest(data_for_bt, ConfluenceStrategy, cash=10000, commission=.002)

            # 백테스트 실행 및 결과 출력
            stats = bt.run()
            print(f"\n--- [{symbol}] 백테스팅 결과 ---")
            print(stats)
            print("---------------------------------\n")

            # 결과 차트를 HTML 파일로 저장 (파일명에 심볼 추가)
            # .plot() 함수는 show=True가 기본값이므로 차트가 자동으로 열립니다.
            bt.plot(filename=f"{symbol}_backtest_result.html")
        else:
            print(f"{symbol} 데이터를 가져오는 데 실패하여 백테스팅을 건너뜁니다.")