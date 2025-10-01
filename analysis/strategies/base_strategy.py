from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseStrategy(ABC):
    """Abstract base class that all strategies should inherit from."""

    @abstractmethod
    def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """Perform analysis and return signal data."""

    @abstractmethod
    def generate_signal(self, analysis_result: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a trading signal from analysis output."""
