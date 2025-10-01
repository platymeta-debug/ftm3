"""Placeholder for RSI Turtle strategy implementation."""

from .base_strategy import BaseStrategy


class RSITurtleStrategy(BaseStrategy):
    """Concrete strategy implementation will arrive in Phase 2."""

    def analyze(self, market_data):
        raise NotImplementedError("RSITurtleStrategy.analyze will be implemented in Phase 2")

    def generate_signal(self, analysis_result):
        raise NotImplementedError("RSITurtleStrategy.generate_signal will be implemented in Phase 2")
