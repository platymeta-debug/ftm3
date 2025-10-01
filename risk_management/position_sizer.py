from binance.client import Client


class PositionSizer:
    """Placeholder position sizing utility."""

    def __init__(self, client: Client) -> None:
        self.client = client
        print("포지션 사이저가 초기화되었습니다.")

    def recommend_size(self, symbol: str, confidence: float) -> float:
        """Return a naive position size recommendation based on confidence."""
        base_size = 0.01
        return round(base_size * max(confidence, 0.1), 4)
