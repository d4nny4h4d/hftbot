"""Strategy engine — orchestrates signal generation, risk checks, and order execution.

HFT BB Reversion: single strategy, config-driven comment prefix and magic number.
"""

import logging
from datetime import datetime, timezone

import yaml

from src.data.market_feed import MarketFeed
from src.data.regime_detector import RegimeDetector, MarketRegime
from src.db.database import Database
from src.execution.mt5_executor import MT5Executor
from src.risk.manager import RiskManager
from src.strategy.base_strategy import BaseStrategy, SignalDirection
from src.strategy.bb_reversion import BBMeanReversion

logger = logging.getLogger(__name__)


class StrategyEngine:
    """Central orchestrator: poll strategy -> check risk -> execute orders -> log trades."""

    def __init__(
        self,
        feed: MarketFeed,
        executor: MT5Executor,
        risk_manager: RiskManager,
        database: Database,
        config_path: str = "config/bot1_london_1pct.yaml",
    ):
        self.feed = feed
        self.executor = executor
        self.risk = risk_manager
        self.db = database
        self.regime = RegimeDetector(config_path)

        cfg = self._load_config(config_path)
        self.strategies: list[BaseStrategy] = []

        # Config-driven identifiers
        general = cfg.get("general", {})
        self._magic = general.get("magic_number", 234567)
        self._comment_prefix = general.get("comment_prefix", "HF1_")
        self._bot_name = general.get("bot_name", "HFTBot")

        # Trade throttling
        self._max_spread = cfg.get("risk_management", {}).get("max_spread_points", 30)
        self._last_trade_time: datetime | None = None
        self._min_trade_interval = cfg.get("risk_management", {}).get("min_time_between_trades_sec", 60)
        self._trades_today = 0
        self._max_trades_day = cfg.get("risk_management", {}).get("max_trades_per_day", 20)
        self._current_date: str | None = None

        # Blocked regimes from strategy config
        strat_cfg = cfg.get("bb_mean_reversion", {})
        self._blocked_regimes = set(strat_cfg.get("blocked_regimes", []))

        # Initialize BB Mean Reversion strategy
        if strat_cfg.get("enabled", True):
            self.strategies.append(BBMeanReversion(strat_cfg))
            logger.info("Strategy loaded: BB Mean Reversion")

        logger.info(
            "Engine initialized: %s | magic=%d | prefix=%s | %d strategies",
            self._bot_name, self._magic, self._comment_prefix, len(self.strategies),
        )

    @staticmethod
    def _load_config(path: str) -> dict:
        with open(path) as f:
            return yaml.safe_load(f)

    # -- Main tick loop --------------------------------------------------------

    def tick(self):
        """Called every check_interval_seconds. Main trading loop iteration."""
        if not self.feed.ensure_connected():
            logger.error("MT5 not connected -- skipping tick")
            return

        tick = self.feed.get_tick()
        if tick is None:
            return

        candles = self.feed.get_candles("M1", 500)
        if candles is None or len(candles) < 50:
            return

        account = self.feed.get_account_info()
        if account is None:
            return

        equity = account["equity"]

        # Track daily trade count
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._trades_today = 0
            self._current_date = today

        self.risk.update_equity_snapshot(equity)

        cb_status = self.risk.check_circuit_breakers(equity)
        if not cb_status["can_trade"]:
            return

        # Detect regime for filtering
        regime = self.regime.detect(candles)

        # Manage existing positions
        self._manage_positions(candles, tick)

        # Check for new signals
        return self._check_signals(candles, tick, equity, regime)

    # -- Signal checking -------------------------------------------------------

    def _check_signals(self, candles, tick, equity, regime):
        """Poll strategy for signals and execute if risk allows."""
        # Trade throttling
        if self._trades_today >= self._max_trades_day:
            return None

        now = datetime.now(timezone.utc)
        if self._last_trade_time:
            elapsed = (now - self._last_trade_time).total_seconds()
            if elapsed < self._min_trade_interval:
                return None

        # Regime filter -- block if current regime is in blocked list
        if regime in self._blocked_regimes:
            return None

        # Spread filter -- skip if spread is too wide (news events, low liquidity)
        if tick and self._max_spread > 0:
            spread_points = tick["spread"] / 0.001  # convert price spread to points
            if spread_points > self._max_spread:
                return None

        symbol_info = self.feed.get_symbol_info()
        if symbol_info is None:
            return None

        for strategy in self.strategies:
            if not strategy.enabled:
                continue

            signal = strategy.generate_signal(candles, tick)
            if signal is None:
                continue

            # Risk check
            allowed, reason = self.risk.can_open_trade(signal.direction.value, equity)
            if not allowed:
                logger.info("Signal rejected by risk manager: %s", reason)
                continue

            # Calculate position size
            effective_risk = self.risk.get_effective_risk_pct(equity)
            lots = self.risk.calculate_lot_size(
                equity=equity,
                sl_distance_price=signal.sl_distance,
                symbol_info=symbol_info,
                risk_override_pct=effective_risk if effective_risk != self.risk.risk_per_trade_pct else None,
            )

            if lots <= 0:
                logger.warning("Calculated lot size is 0 -- skipping trade")
                continue

            # Execute the trade with config-driven comment prefix and magic
            comment = f"{self._comment_prefix}{signal.strategy_name}"
            result = self.executor.open_trade(
                direction=signal.direction.value,
                lots=lots,
                sl_price=signal.sl_price,
                tp_price=signal.tp_price,
                comment=comment,
                magic=self._magic,
            )

            if result:
                self.db.log_trade_open(
                    ticket=result["ticket"],
                    symbol=result["symbol"],
                    direction=result["direction"],
                    lots=lots,
                    entry_price=result["entry_price"],
                    sl=signal.sl_price,
                    tp=signal.tp_price,
                    strategy=signal.strategy_name,
                    equity_at_entry=equity,
                    comment=signal.reason,
                )

                self._last_trade_time = now
                self._trades_today += 1

                logger.info(
                    "TRADE EXECUTED: %s %s %.2f lots @ %.2f | SL: %.2f | TP: %.2f | %s",
                    signal.direction.value.upper(), self.feed.symbol,
                    lots, result["entry_price"], signal.sl_price,
                    signal.tp_price, signal.strategy_name,
                )

                return {
                    "type": "trade_opened",
                    "signal": signal,
                    "result": result,
                    "lots": lots,
                    "risk_pct": effective_risk,
                    "regime": regime,
                }

        return None

    # -- Position management ---------------------------------------------------

    def _manage_positions(self, candles, tick):
        """Check open positions for time-based exits."""
        positions = self.executor.get_open_positions()

        for pos in positions:
            # Only manage positions belonging to this bot (by comment prefix)
            if not pos.get("comment", "").startswith(self._comment_prefix):
                continue

            strategy_name = pos["comment"].replace(self._comment_prefix, "")
            strategy = self._get_strategy(strategy_name)
            if strategy is None:
                continue

            if strategy.should_close(pos, candles, tick):
                self.executor.close_trade(
                    pos["ticket"],
                    comment=f"{self._comment_prefix}time_exit",
                )
                self._log_position_close(pos)

    def _get_strategy(self, name: str) -> BaseStrategy | None:
        for s in self.strategies:
            if s.name == name:
                return s
        return None

    def _log_position_close(self, position: dict):
        account = self.feed.get_account_info()
        equity = account["equity"] if account else 0
        self.db.log_trade_close(
            ticket=position["ticket"],
            exit_price=position["current_price"],
            profit_usd=position["profit"],
            equity_at_exit=equity,
        )

    # -- Status ----------------------------------------------------------------

    def get_status(self) -> dict:
        account = self.feed.get_account_info()
        equity = account["equity"] if account else 0
        return {
            "bot_name": self._bot_name,
            "connected": self.feed.is_connected(),
            "equity": equity,
            "regime": self.regime.current_regime,
            "adx": round(self.regime.current_adx, 1),
            "active_strategies": [s.name for s in self.strategies if s.enabled],
            "open_positions": sum(
                1 for p in self.executor.get_open_positions()
                if p.get("comment", "").startswith(self._comment_prefix)
            ),
            "circuit_breaker": self.risk.check_circuit_breakers(equity),
            "daily_pnl_pct": round(self.risk.get_daily_pnl_pct(equity), 2),
            "trades_today": self._trades_today,
        }
