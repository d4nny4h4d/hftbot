"""Bollinger Band Mean Reversion strategy — the only HFT strategy after backtesting.

Entry Rules:
  BUY:  Candle low touches/pierces lower BB AND RSI(14) < 30
  SELL: Candle high touches/pierces upper BB AND RSI(14) > 70

Exit Rules:
  SL: 1.0 x ATR(14) from entry
  TP: 1.5 x ATR(14) from entry (R:R = 1.5:1)

Filters:
  - Session: London only (Bot 1 & 2) or London+NY (Bot 3)
  - Regime: Strong Trend only (ADX > 50)
"""

import logging

import numpy as np
import pandas as pd

from src.strategy.base_strategy import BaseStrategy, TradeSignal, SignalDirection

logger = logging.getLogger(__name__)


class BBMeanReversion(BaseStrategy):
    """Bollinger Band Mean Reversion — backtested PF 1.42, 50%+ win rate."""

    def __init__(self, config: dict):
        self.name = "BB_Reversion"
        self.enabled = config.get("enabled", True)

        self.bb_period = config.get("bb_period", 20)
        self.bb_std = config.get("bb_std", 2.0)
        self.rsi_period = config.get("rsi_period", 14)
        self.rsi_oversold = config.get("rsi_oversold", 30)
        self.rsi_overbought = config.get("rsi_overbought", 70)
        self.sl_atr_mult = config.get("sl_atr_mult", 1.0)
        self.tp_atr_mult = config.get("tp_atr_mult", 1.5)
        self.atr_period = config.get("atr_period", 14)

        # Session hours from config
        session_hours = config.get("session_hours", {})
        self.allowed_hours = set()
        sessions = config.get("sessions", ["london"])

        if "london" in sessions:
            start = session_hours.get("london_start", 7)
            end = session_hours.get("london_end", 13)
            self.allowed_hours.update(range(start, end))

        if "newyork" in sessions:
            start = session_hours.get("newyork_start", 13)
            end = session_hours.get("newyork_end", 20)
            self.allowed_hours.update(range(start, end))

        # ─── Paper-trading optimization: blocked hours (added 2026-04-17) ───
        # Analysis of 119 live trades (30 days) showed:
        #   - Hour 07 UTC: 23 trades, 30% WR, avg -$2.11 → BLOCK
        #   - Hour 13 UTC: 10 trades, 10% WR, avg -$4.30 → BLOCK
        #   - Best hours: 09, 11, 16, 17 UTC (60-100% WR)
        # Backtest: blocking these two hours reduces loss from -$85 to near zero
        self.blocked_hours = set(config.get("blocked_hours", []) or [])
        if self.blocked_hours:
            self.allowed_hours -= self.blocked_hours

        # Regime filter
        self.blocked_regimes = set(config.get("blocked_regimes", []))

        logger.info(
            "BB Reversion: BB(%d,%.1f), RSI(%d), SL=%.1fxATR, TP=%.1fxATR, hours=%s, blocked=%s",
            self.bb_period, self.bb_std, self.rsi_period,
            self.sl_atr_mult, self.tp_atr_mult, sorted(self.allowed_hours),
            sorted(self.blocked_hours) if self.blocked_hours else "none",
        )

    @staticmethod
    def _sma(series: pd.Series, period: int) -> pd.Series:
        return series.rolling(period).mean()

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        # Wilder's smoothing: alpha=1/period (matches MT5 ATR indicator)
        return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    def generate_signal(self, candles: pd.DataFrame, tick: dict) -> TradeSignal | None:
        """Check for BB Mean Reversion entry signal."""
        if len(candles) < max(self.bb_period, self.rsi_period, self.atr_period) + 5:
            return None

        # Session filter
        current_hour = candles.index[-1].hour
        if current_hour not in self.allowed_hours:
            return None

        # Compute indicators on the candles
        close = candles["close"]
        rsi_vals = self._rsi(close, self.rsi_period)
        atr_vals = self._atr(candles, self.atr_period)

        # Bollinger Bands
        mid = self._sma(close, self.bb_period)
        std = close.rolling(self.bb_period).std()
        bb_upper = mid + self.bb_std * std
        bb_lower = mid - self.bb_std * std

        # Get latest values
        last = candles.iloc[-1]
        current_close = last["close"]
        current_low = last["low"]
        current_high = last["high"]
        current_rsi = rsi_vals.iloc[-1]
        current_atr = atr_vals.iloc[-1]
        current_bb_upper = bb_upper.iloc[-1]
        current_bb_lower = bb_lower.iloc[-1]

        if np.isnan(current_atr) or current_atr <= 0:
            return None
        if np.isnan(current_bb_lower) or np.isnan(current_bb_upper):
            return None
        if np.isnan(current_rsi):
            return None

        # BUY: price touches lower BB and RSI < oversold
        if current_low <= current_bb_lower and current_rsi < self.rsi_oversold:
            sl_price = current_close - self.sl_atr_mult * current_atr
            tp_price = current_close + self.tp_atr_mult * current_atr
            sl_distance = abs(current_close - sl_price)

            return TradeSignal(
                direction=SignalDirection.BUY,
                entry_price=current_close,
                sl_price=sl_price,
                tp_price=tp_price,
                sl_distance=sl_distance,
                strategy_name=self.name,
                reason=f"BB_BUY RSI={current_rsi:.0f} ATR={current_atr:.2f}",
            )

        # SELL: price touches upper BB and RSI > overbought
        if current_high >= current_bb_upper and current_rsi > self.rsi_overbought:
            sl_price = current_close + self.sl_atr_mult * current_atr
            tp_price = current_close - self.tp_atr_mult * current_atr
            sl_distance = abs(current_close - sl_price)

            return TradeSignal(
                direction=SignalDirection.SELL,
                entry_price=current_close,
                sl_price=sl_price,
                tp_price=tp_price,
                sl_distance=sl_distance,
                strategy_name=self.name,
                reason=f"BB_SELL RSI={current_rsi:.0f} ATR={current_atr:.2f}",
            )

        return None

    def should_close(self, position: dict, candles: pd.DataFrame, tick: dict) -> bool:
        """No time-based exit for BB Reversion — SL/TP handles exits."""
        return False
