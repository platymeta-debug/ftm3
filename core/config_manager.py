# core/config_manager.py (최종 수정본)

import os
import json
from typing import Dict, List
from dotenv import load_dotenv

# .env 파일 로드는 main.py에서 한 번만 실행하므로 여기서는 호출하지 않습니다.

class ConfigManager:
    """
    .env 파일과 JSON 설정 파일을 결합하여 모든 설정을 관리하는 클래스.
    이 클래스는 객체 생성 없이 독립적으로 존재하며, main.py에서 생성하여 필요한 곳에 전달합니다.
    """
    def __init__(self) -> None:
        # --- 1. .env 파일에서 환경 및 인프라 설정 로드 ---
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

        # --- 2. 전략의 기본 구조 설정 (하드코딩 또는 .env) ---
        self.analysis_timeframes = ["1d", "4h", "1h", "15m"]
        self.tf_vote_weights = [4.0, 3.0, 2.0, 1.0]
        self.trend_entry_confirm_count = 3
        self.market_regime_adx_th = 20.0

        # --- 3. 최적화된 JSON 설정 파일 로드 ---
        self.optimized_strategy_configs = {}
        try:
            with open("strategies_optimized.json", "r", encoding="utf-8") as f:
                self.optimized_strategy_configs = json.load(f)
            print("✅ strategies_optimized.json 최적화 지표 설정 파일을 성공적으로 로드했습니다.")
        except (FileNotFoundError, json.JSONDecodeError):
            print("⚠️ strategies_optimized.json을 찾을 수 없어 기본 설정을 사용합니다.")
            self.optimized_strategy_configs = {"BULL": {}, "BEAR": {}, "SIDEWAYS": {}}

        self.optimal_settings = {}
        try:
            with open("optimal_settings.json", "r", encoding="utf-8") as f:
                self.optimal_settings = json.load(f)
            print("✅ optimal_settings.json 최적화 실행 설정 파일을 성공적으로 로드했습니다.")
        except (FileNotFoundError, json.JSONDecodeError):
            print("⚠️ optimal_settings.json을 찾을 수 없습니다. 기본 실행 파라미터를 사용합니다.")

        # --- 4. 안전장치: 설정 파일이 없을 경우 사용할 기본값 ---
        self.default_strategy_params = {
            "OPEN_TH": 12.0,
            "RR_RATIO": 2.0,
            "SL_ATR_MULTIPLIER": 1.5
        }
        print("💡 모든 설정 로드가 완료되었습니다.")


    def get_strategy_params(self, symbol: str, market_regime: str = "BULL") -> Dict:
        """
        주어진 시장 상황과 심볼에 맞는 최적화된 실행 파라미터를 반환합니다.
        optimal_settings.json의 에피소드별(날짜 키) 설정까지 고려합니다.
        """
        regime_upper = market_regime.upper()
        # 해당 레짐, 해당 심볼의 설정 가져오기
        optimized_by_symbol = self.optimal_settings.get(regime_upper, {}).get(symbol, {})

        # 날짜 기반 에피소드 키가 있는지 확인 (e.g., "2023-01-01_2023-06-30")
        episode_keys = [k for k in optimized_by_symbol.keys() if '_' in k and len(k.split('_')) == 2]
        
        final_params = {}
        if episode_keys:
            # 가장 최신 에피소드의 설정을 사용 (단순 문자열 정렬 기준)
            latest_episode_key = sorted(episode_keys)[-1]
            final_params = optimized_by_symbol.get(latest_episode_key, {})
        elif optimized_by_symbol:
            # 에피소드 키가 없는 구버전 형식 지원
            final_params = optimized_by_symbol

        if final_params:
            # 찾은 설정이 있더라도, 누락된 값이 있을 수 있으므로 기본값으로 채워줍니다.
            return {
                "open_th": final_params.get("OPEN_TH", self.default_strategy_params["OPEN_TH"]),
                "risk_reward_ratio": final_params.get("RR_RATIO", self.default_strategy_params["RR_RATIO"]),
                "sl_atr_multiplier": final_params.get("SL_ATR_MULTIPLIER", self.default_strategy_params["SL_ATR_MULTIPLIER"])
            }
        else:
            # 최적화된 설정이 전혀 없으면 기본값을 반환합니다.
            return self.default_strategy_params

    def get_strategy_configs(self, market_regime: str) -> Dict:
        """
        주어진 시장 상황에 맞는 최적화된 '지표' 파라미터 묶음을 반환합니다.
        """
        regime_upper = market_regime.upper()
        return self.optimized_strategy_configs.get(regime_upper, self.optimized_strategy_configs.get("BULL", {}))

    def get_exec_policy(self, symbol: str, regime: str | None = "BULL") -> dict:
        """
        TradingEngine에서 사용할 상세 실행 정책을 반환합니다.
        """
        regime_upper = (regime or "BULL").upper()
        settings_by_symbol = self.optimal_settings.get(regime_upper, {}).get(symbol, {})
        
        best = {}
        episode_keys = [k for k in settings_by_symbol.keys() if '_' in k]
        if episode_keys:
            latest_key = sorted(episode_keys)[-1]
            best = settings_by_symbol.get(latest_key, {})
        elif settings_by_symbol:
            best = settings_by_symbol

        # 문자열로 된 partial 값을 리스트로 변환
        partial = best.get("exec_partial", [])
        if isinstance(partial, str):
            parts = [x.strip() for x in partial.split(",") if x.strip()]
            partial = [float(x) for x in parts]

        return {
            "sl_atr_multiplier": float(best.get("SL_ATR_MULTIPLIER", 1.5)),
            "risk_reward_ratio": float(best.get("RR_RATIO", 2.0)),
            "exec_partial": partial,
            "exec_time_stop_bars": int(best.get("exec_time_stop_bars", 0)),
            "exec_trailing_mode": best.get("exec_trailing_mode", "off"),
            "exec_trailing_k": float(best.get("exec_trailing_k", 0.0)),
            "risk_per_trade": float(best.get("risk_per_trade", 0.01)),
            "max_exposure_frac": float(best.get("max_exposure_frac", 0.30)),
        }

# [중요] 파일 하단에 config 객체를 직접 생성하는 코드가 있었다면 반드시 삭제해야 합니다.
# config = ConfigManager() # <- 이와 같은 줄이 있다면 삭제 또는 주석 처리
