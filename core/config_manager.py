import os
from typing import List, Optional

from dotenv import load_dotenv


class ConfigManager:
    """Centralized configuration loader for environment variables."""

    def __init__(self) -> None:
        load_dotenv()
        print("환경 변수 파일(.env)을 로드했습니다.")

        # --- Core Modes ---
        self.trade_mode = os.getenv("TRADE_MODE", "testnet")
        self.is_testnet = self.trade_mode == "testnet"

        # --- Binance Keys ---
        if self.is_testnet:
            self.api_key = os.getenv("BINANCE_TEST_API_KEY")
            self.api_secret = os.getenv("BINANCE_TEST_API_SECRET")
        else:
            self.api_key = os.getenv("BINANCE_LIVE_API_KEY")
            self.api_secret = os.getenv("BINANCE_LIVE_API_SECRET")

        # --- Discord ---
        self.discord_bot_token = os.getenv("DISCORD_BOT_TOKEN")
        self.alerts_channel_id = self._get_int("DISCORD_CHANNEL_ID_ALERTS")

        # --- Analysis Timeframes & Weights ---
        self.timeframes = self._get_list("ANALYSIS_TIMEFRAMES")
        if not self.timeframes:
            self.timeframes = self._get_list("ANLYSIS_TIMEFRAMES", ["15m", "1h", "4h", "1d"])

        self.tf_vote_weights = self._get_list_float("TF_VOTE_WEIGHTS")
        if not self.tf_vote_weights:
            self.tf_vote_weights = [1.0, 2.0, 3.0, 4.0]

        self.symbols = self._get_list("SYMBOLS", ["BTCUSDT"])
        self.open_threshold = self._get_float("OPEN_TH", 10.0)
        self.trade_quantity = self._get_float("TRADE_QUANTITY", 0.001)

        # --- Database ---
        self.db_path = os.getenv("DB_PATH", "./runtime/trader.db")

    def _get_bool(self, key: str, default: bool = False) -> bool:
        val = os.getenv(key)
        if val is None:
            return default
        return val.strip().lower() in {"true", "1", "t", "yes"}

    def _get_int(self, key: str, default: int = 0) -> int:
        val = os.getenv(key)
        if val is None:
            return default
        try:
            return int(val.strip())
        except (ValueError, TypeError):
            return default

    def _get_float(self, key: str, default: float = 0.0) -> float:
        val = os.getenv(key)
        if val is None:
            return default
        try:
            return float(val.strip())
        except (ValueError, TypeError):
            return default

    def _get_list(self, key: str, default: Optional[List[str]] = None) -> List[str]:
        val = os.getenv(key)
        if val is None:
            return list(default) if default is not None else []
        return [item.strip() for item in val.split(",") if item.strip()]

    def _get_list_float(self, key: str, default: Optional[List[float]] = None) -> List[float]:
        val = os.getenv(key)
        if val is None:
            return list(default) if default is not None else []
        floats: List[float] = []
        for item in val.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                floats.append(float(item))
            except ValueError:
                continue
        return floats


# 프로그램 전체에서 공유할 단일 설정 객체 생성
config = ConfigManager()
