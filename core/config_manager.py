import os
from typing import List, Optional
from dotenv import load_dotenv

class ConfigManager:
    def __init__(self) -> None:
        load_dotenv()
        print("환경 변수 파일(.env)을 로드했습니다.")

        # Core Settings
        self.trade_mode = os.getenv("TRADE_MODE", "testnet")
        self.is_testnet = self.trade_mode == "testnet"
        self.exec_active = self._get_bool("EXEC_ACTIVE", False)
        self.symbols = self._get_list("SYMBOLS", ["BTCUSDT"])

        # Binance Keys
        if self.is_testnet:
            self.api_key = os.getenv("BINANCE_TEST_API_KEY")
            self.api_secret = os.getenv("BINANCE_TEST_API_SECRET")
        else:
            self.api_key = os.getenv("BINANCE_LIVE_API_KEY")
            self.api_secret = os.getenv("BINANCE_LIVE_API_SECRET")

        # Analysis Engine
        self.analysis_timeframes = self._get_list("ANALYSIS_TIMEFRAMES", ["1d", "4h", "1h", "15m"])
        self.tf_vote_weights = self._get_list_float("TF_VOTE_WEIGHTS", [4.0, 3.0, 2.0, 1.0])
        self.open_th = self._get_float("OPEN_TH", 12.0)
        self.market_regime_adx_th = self._get_float("MARKET_REGIME_ADX_TH", 23.0)

        # Signal Quality Rules
        self.quality_min_avg_score = self._get_float("QUALITY_MIN_AVG_SCORE", 15.0)
        self.quality_max_std_dev = self._get_float("QUALITY_MAX_STD_DEV", 3.0)

        # Trading Logic Rules
        self.trend_entry_confirm_count = self._get_int("TREND_ENTRY_CONFIRM_COUNT", 3)
        self.sideways_rsi_confirm_count = self._get_int("SIDEWAYS_RSI_CONFIRM_COUNT", 2)
        self.sideways_rsi_oversold = self._get_float("SIDEWAYS_RSI_OVERSOLD", 35.0)
        self.sideways_rsi_overbought = self._get_float("SIDEWAYS_RSI_OVERBOUGHT", 65.0)
        self.reversal_confirm_count = self._get_int("REVERSAL_CONFIRM_COUNT", 2)

        # --- ▼▼▼ V4 수정 사항 ▼▼▼ ---
        # Risk Management
        self.aggr_level = self._get_int("AGGR_LEVEL", 3)
        self.risk_target_pct = self._get_float("RISK_TARGET_PCT", 0.02)
        self.sl_atr_multiplier = self._get_float("SL_ATR_MULTIPLIER", 1.5)
        self.risk_reward_ratio = self._get_float("RISK_REWARD_RATIO", 2.0) # take_profit_pct 대체

        # Risk Scales
        self.risk_scale_high = self._get_float("RISK_SCALE_HIGH_CONFIDENCE", 1.5)
        self.risk_scale_medium = self._get_float("RISK_SCALE_MEDIUM_CONFIDENCE", 1.0)
        self.risk_scale_low = self._get_float("RISK_SCALE_LOW_CONFIDENCE", 0.5)
        # --- ▲▲▲ V4 수정 사항 ▲▲▲ ---

        # Leverage Map
        self.leverage_map = {
            "BTCUSDT": {
                "LOW": self._get_int("LEVERAGE_BTCUSDT_LOW", 5),
                "MID": self._get_int("LEVERAGE_BTCUSDT_MID", 10),
                "HIGH": self._get_int("LEVERAGE_BTCUSDT_HIGH", 20),
            },
            "ETHUSDT": {
                "LOW": self._get_int("LEVERAGE_ETHUSDT_LOW", 4),
                "MID": self._get_int("LEVERAGE_ETHUSDT_MID", 8),
                "HIGH": self._get_int("LEVERAGE_ETHUSDT_HIGH", 15),
            },
        }

        # Adaptive Logic & Portfolio
        self.adaptive_aggr_enabled = self._get_bool("ADAPTIVE_AGGR_ENABLED", True)
        self.adaptive_volatility_threshold = self._get_float("ADAPTIVE_VOLATILITY_THRESHOLD", 0.04)
        self.max_open_positions = self._get_int("MAX_OPEN_POSITIONS", 2)

        # Infrastructure
        self.db_path = os.getenv("DB_PATH", "./runtime/trader.db")
        self.discord_bot_token = os.getenv("DISCORD_BOT_TOKEN")
        self.panel_channel_id = self._get_int("DISCORD_PANEL_CHANNEL_ID")
        self.analysis_channel_id = self._get_int("DISCORD_ANALYSIS_CHANNEL_ID")
        self.alerts_channel_id = self._get_int("DISCORD_ALERTS_CHANNEL_ID")
        # Discord Channel IDs
        self.dashboard_channel_id = self._get_int("DISCORD_CHANNEL_ID_DASHBOARD")
        self.alerts_channel_id = self._get_int("DISCORD_CHANNEL_ID_ALERTS")
        self.analysis_channel_id = self._get_int("DISCORD_CHANNEL_ID_ANALYSIS")
        self.panel_channel_id = self._get_int("DISCORD_CHANNEL_ID_PANEL")

    def _get_bool(self, key: str, default: bool = False) -> bool:
        val = os.getenv(key)
        if val is None: return default
        return val.strip().lower() in {"true", "1", "t", "yes"}

    def _get_int(self, key: str, default: int = 0) -> int:
        val = os.getenv(key)
        if val is None: return default
        try: return int(val.strip())
        except (ValueError, TypeError): return default

    def _get_float(self, key: str, default: float = 0.0) -> float:
        val = os.getenv(key)
        if val is None: return default
        try: return float(val.strip())
        except (ValueError, TypeError): return default

    def _get_list(self, key: str, default: Optional[List[str]] = None) -> List[str]:
        val = os.getenv(key)
        if val is None: return list(default) if default is not None else []
        return [item.strip() for item in val.split(",") if item.strip()]

    def _get_list_float(self, key: str, default: Optional[List[float]] = None) -> List[float]:
        val = os.getenv(key)
        if val is None: return list(default) if default is not None else []
        floats = []
        for item in val.split(","):
            if item.strip():
                try: floats.append(float(item.strip()))
                except ValueError: continue
        return floats

config = ConfigManager()
