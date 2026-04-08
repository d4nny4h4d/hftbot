"""Abstract base class for all HFT trading strategies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class SignalDirection(Enum):
    BUY = "buy"
    SELL = "sell"
    NONE = "none"


@dataclass
class TradeSignal:
    """A signal emitted by a strategy."""
    direction: SignalDirection
    entry_price: float
    sl_price: float
    tp_price: float
    sl_distance: float
    strategy_name: str
    confidence: float = 1.0
    reason: str = ""


class BaseStrategy(ABC):
    """All HFT strategies must implement this interface."""

    name: str = ""
    enabled: bool = True

    @abstractmethod
    def generate_signal(self, candles: pd.DataFrame, tick: dict) -> TradeSignal | None:
        """Analyze candles and current tick, return a signal or None."""
        ...

    @abstractmethod
    def should_close(self, position: dict, candles: pd.DataFrame, tick: dict) -> bool:
        """Check if an open position from this strategy should be closed."""
        ...
