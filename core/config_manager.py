# core/config_manager.py (최종 완성본)

import os
import json
from typing import Dict, List
from dotenv import load_dotenv

class ConfigManager:
    """
    .env 파일과 JSON 설정 파일을 결합하여 모든 설정을 관리하는 최종 클래스.
    """
    def __init__(self) -> None:
        # --- 1. .env에서 환경 및 인프라 설정 로드 ---
        self.trade_mode = os.getenv("TRADE_MODE", "testnet")
        self.is_testnet = self.trade_mode == "testnet"
        self.exec_active = os.getenv("EXEC_ACTIVE", "true").lower() in ("true", "1", "t")
        self.symbols = [symbol.strip() for symbol in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(',')]
        self.max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS", 2))

        # --- ▼▼▼ [핵심 수정] 누락된 설정값 추가 ▼▼▼ ---
        self.aggr_level = int(os.getenv("AGGR_LEVEL", 5)) # 기본 공격성 레벨 (기본값: 5)
        self.adaptive_aggr_enabled = os.getenv("ADAPTIVE_AGGR_ENABLED", "true").lower() in ("true", "1", "t")
        # --- ▲▲▲ [핵심 수정] ---

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

        # --- 2. 전략의 기본 구조 설정 ---
        self.analysis_timeframes = ["1d", "4h", "1h", "15m"]
        self.tf_vote_weights = [4.0, 3.0, 2.0, 1.0]
        self.trend_entry_confirm_count = 3
        self.market_regime_adx_th = 20.0
        
        # --- ▼▼▼ [추가] 서킷 브레이커 및 변동성 설정 추가 ▼▼▼ ---
        self.circuit_breaker_enabled = os.getenv("CIRCUIT_BREAKER_ENABLED", "true").lower() in ("true", "1", "t")
        self.drawdown_check_days = int(os.getenv("DRAWDOWN_CHECK_DAYS", 7))
        self.drawdown_threshold_pct = float(os.getenv("DRAWDOWN_THRESHOLD_PCT", 10.0))
        self.adaptive_volatility_threshold = float(os.getenv("ADAPTIVE_VOLATILITY_THRESHOLD", 0.05)) # 5%

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
            "OPEN_TH": 12.0, "RR_RATIO": 2.0, "SL_ATR_MULTIPLIER": 1.5
        }
        print("💡 모든 설정 로드가 완료되었습니다.")

    def get_strategy_params(self, symbol: str, market_regime: str = "BULL") -> Dict:
        """
        [수정된 로직] optimal_settings.json의 구조가 에피소드 기반이든, 단일 설정이든
        안전하게 파라미터를 읽어옵니다.
        """
        regime_upper = market_regime.upper()
        # 1. 심볼에 해당하는 설정 부분을 가져옵니다.
        params_for_symbol = self.optimal_settings.get(regime_upper, {}).get(symbol, {})

        # 2. 설정이 없으면 기본값을 반환합니다.
        if not params_for_symbol:
            return self.default_strategy_params

        # 3. 설정 내부에 에피소드 키(예: '2025-01-01_2025-05-10')가 있는지 확인합니다.
        episode_keys = []
        if isinstance(params_for_symbol, dict):
            # 'VALUE', 'OPTIMIZED_METRIC' 같은 키는 에피소드 키가 아니므로 걸러냅니다.
            episode_keys = [
                k for k in params_for_symbol.keys()
                if isinstance(k, str) and '_' in k and k not in ["OPTIMIZED_METRIC"]
            ]

        final_params = {}
        if isinstance(params_for_symbol, dict):
            episode_keys = [k for k in params_for_symbol.keys() if isinstance(k, str) and '_' in k]
            if episode_keys:
                latest_episode_key = sorted(episode_keys)[-1]
                final_params = params_for_symbol.get(latest_episode_key, {})
            else:
                final_params = params_for_symbol
        
        # final_params가 딕셔너리가 아니거나 비어있을 경우를 대비
        if not isinstance(final_params, dict) or not final_params:
            print(f"⚠️ [{symbol}/{regime_upper}] 최적화된 파라미터를 찾을 수 없습니다. 기본값을 사용합니다.")
            # 기본값 딕셔너리의 키를 반환 형식에 맞게 변환하여 전달
            return {
                "open_th": self.default_strategy_params.get("OPEN_TH"),
                "risk_reward_ratio": self.default_strategy_params.get("RR_RATIO"),
                "sl_atr_multiplier": self.default_strategy_params.get("SL_ATR_MULTIPLIER"),
            }

    def get_strategy_configs(self, market_regime: str) -> Dict:
        regime_upper = market_regime.upper()
        return self.optimized_strategy_configs.get(regime_upper, self.optimized_strategy_configs.get("BULL", {}))

    def get_exec_policy(self, symbol: str, regime: str | None = "BULL") -> dict:
        regime_upper = (regime or "BULL").upper()
        settings_by_symbol = self.optimal_settings.get(regime_upper, {}).get(symbol, {})
        best = {}
        episode_keys = [k for k in settings_by_symbol.keys() if '_' in k]
        if episode_keys:
            latest_key = sorted(episode_keys)[-1]
            best = settings_by_symbol.get(latest_key, {})
        elif settings_by_symbol:
            best = settings_by_symbol
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
