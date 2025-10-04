# core/config_manager.py (최종 완성본 - optimal_settings.json 중심)

import os
import json
from typing import Dict, List
from dotenv import load_dotenv

class ConfigManager:
    """
    .env 파일에서 환경 설정을, optimal_settings.json에서 전략 설정을 불러와 결합하는
    최종 설정 관리자 클래스.
    """
    def __init__(self) -> None:
        load_dotenv()
        print("✅ .env 환경 변수 파일을 로드했습니다.")

        # --- 1. .env에서 환경 및 인프라 설정 로드 ---
        self.trade_mode = os.getenv("TRADE_MODE", "testnet")
        self.is_testnet = self.trade_mode == "testnet"
        self.exec_active = os.getenv("EXEC_ACTIVE", "true").lower() in ("true", "1", "t")
        self.symbols = [symbol.strip() for symbol in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(',')]
        self.max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS", 2))

        # API Keys
        if self.is_testnet:
            self.api_key = os.getenv("BINANCE_TEST_API_KEY")
            self.api_secret = os.getenv("BINANCE_TEST_API_SECRET")
        else:
            self.api_key = os.getenv("BINANCE_LIVE_API_KEY")
            self.api_secret = os.getenv("BINANCE_LIVE_API_SECRET")
        self.fred_api_key = os.getenv("FRED_API_KEY")

        # Discord
        self.discord_bot_token = os.getenv("DISCORD_BOT_TOKEN")
        self.panel_channel_id = int(os.getenv("DISCORD_PANEL_CHANNEL_ID", 0))
        self.analysis_channel_id = int(os.getenv("DISCORD_ANALYSIS_CHANNEL_ID", 0))
        self.alerts_channel_id = int(os.getenv("DISCORD_ALERTS_CHANNEL_ID", 0))

        # Database
        self.db_path = os.getenv("DB_PATH", "./runtime/trader.db")

        # --- 2. .env 또는 하드코딩된 '전략의 뼈대' 설정 ---
        # 이 값들은 최적화 대상이 아닌, 전략의 기본 구조를 정의합니다.
        self.analysis_timeframes = ["1d", "4h", "1h", "15m"]
        self.tf_vote_weights = [4.0, 3.0, 2.0, 1.0]
        self.trend_entry_confirm_count = 3
        self.market_regime_adx_th = 20.0 # ADX 기반 기술적 추세 판단 기준

        self.optimized_strategy_configs = {}
        try:
            with open("strategies_optimized.json", "r", encoding="utf-8") as f:
                self.optimized_strategy_configs = json.load(f)
            print("✅ strategies_optimized.json 최적화 지표 설정 파일을 성공적으로 로드했습니다.")
        except (FileNotFoundError, json.JSONDecodeError):
            print("⚠️ strategies_optimized.json을 찾을 수 없어 기본 strategies.json을 사용합니다.")
            try:
                with open("strategies.json", "r", encoding="utf-8") as f:
                    # fallback으로 기본 strategies.json을 BULL/BEAR/SIDEWAYS 구조로 변환
                    base_config = json.load(f)
                    self.optimized_strategy_configs = {
                        "BULL": base_config,
                        "BEAR": base_config,
                        "SIDEWAYS": base_config
                    }
                print("✅ 기본 strategies.json 파일을 로드했습니다.")
            except (FileNotFoundError, json.JSONDecodeError):
                 print("🚨 어떤 strategies 파일도 찾을 수 없습니다. 지표 분석이 기본값으로 실행됩니다.")
                 self.optimized_strategy_configs = {"BULL": {}, "BEAR": {}, "SIDEWAYS": {}}

        # --- 4. 안전장치: '최적화된 전략'이 없을 경우 사용할 기본값(Fallback) ---
        self.default_strategy_params = {
            "OPEN_TH": 12.0,
            "RR_RATIO": 2.0,
            "SL_ATR_MULTIPLIER": 1.5
        }
        print("💡 모든 전략 파라미터는 이제 optimal_settings.json을 기준으로 작동합니다.")


    def get_strategy_params(self, symbol: str, market_regime: str) -> Dict:
        """
        주어진 시장 상황에 맞는 최적화된 파라미터를 반환합니다.
        값이 없으면 안전을 위해 설정된 기본값(Default)을 사용합니다.
        """
        regime_upper = market_regime.upper()
        # optimal_settings.json에서 해당 시장, 해당 심볼의 최적값을 찾아본다
        optimized = self.optimal_settings.get(regime_upper, {}).get(symbol)

        if optimized:
            print(f"✅ [{regime_upper}/{symbol}] 최적화 파라미터 적용: {optimized}")
            # optimal_settings.json에 값이 있어도, 일부 값이 누락될 경우를 대비해 기본값으로 채워줌
            return {
                "open_th": optimized.get("OPEN_TH", self.default_strategy_params["OPEN_TH"]),
                "risk_reward_ratio": optimized.get("RR_RATIO", self.default_strategy_params["RR_RATIO"]),
                "sl_atr_multiplier": optimized.get("SL_ATR_MULTIPLIER", self.default_strategy_params["SL_ATR_MULTIPLIER"])
            }
        else:
            print(f"⚠️ [{regime_upper}/{symbol}] 최적화된 설정값이 없습니다. 안전을 위해 기본값을 사용합니다.")
            return self.default_strategy_params
        
    def get_strategy_configs(self, market_regime: str) -> Dict:
        """
        주어진 시장 상황(BULL, BEAR, SIDEWAYS)에 맞는 최적화된 지표 파라미터 묶음을 반환합니다.
        값이 없으면 BULL 마켓 설정을 기본값(Fallback)으로 사용합니다.
        """
        regime_upper = market_regime.upper()
        # 해당 시장의 설정이 있으면 그것을, 없으면 BULL 마켓 설정을, 그것도 없으면 빈 딕셔너리를 반환
        return self.optimized_strategy_configs.get(regime_upper, self.optimized_strategy_configs.get("BULL", {}))
    
# 단일 ConfigManager 인스턴스 생성
config = ConfigManager()
