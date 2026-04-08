"""Percentage-based risk manager -- position sizing, circuit breakers, drawdown tracking."""

import logging
from datetime import datetime, timezone

import MetaTrader5 as mt5
import yaml

logger = logging.getLogger(__name__)


class RiskManager:
    """All risk parameters are percentage-based. No dollar amounts in logic."""

    def __init__(self, config_path: str = "config/bot1_london_1pct.yaml"):
        cfg = self._load_config(config_path)
        rm = cfg["risk_management"]

        self.risk_per_trade_pct = rm["risk_per_trade_pct"]
        self.daily_loss_limit_pct = rm["daily_loss_limit_pct"]
        self.weekly_loss_limit_pct = rm["weekly_loss_limit_pct"]
        self.monthly_drawdown_alert_pct = rm["monthly_drawdown_alert_pct"]
        self.absolute_max_drawdown_pct = rm["absolute_max_drawdown_pct"]
        self.max_open_positions = rm["max_open_positions"]
        self.max_same_direction = rm["max_same_direction"]
        self.min_lot_size = rm["min_lot_size"]
        self.max_lot_size = rm["max_lot_size"]

        # Bot-specific filtering -- only count this bot's own positions
        general = cfg.get("general", {})
        self._comment_prefix = general.get("comment_prefix", "")
        self._magic_number = general.get("magic_number", 0)

        self._day_start_equity: float | None = None
        self._day_start_date: datetime | None = None
        self._peak_equity: float | None = None
        self._circuit_breaker_active = False

    @staticmethod
    def _load_config(path: str) -> dict:
        with open(path) as f:
            return yaml.safe_load(f)

    # -- Equity tracking -------------------------------------------------------

    def update_equity_snapshot(self, current_equity: float):
        now = datetime.now(timezone.utc)

        if self._day_start_date is None or now.date() != self._day_start_date.date():
            self._day_start_equity = current_equity
            self._day_start_date = now
            self._circuit_breaker_active = False
            logger.info("New trading day -- start equity: $%.2f", current_equity)

        if self._peak_equity is None or current_equity > self._peak_equity:
            self._peak_equity = current_equity

    def get_daily_pnl_pct(self, current_equity: float) -> float:
        if self._day_start_equity is None or self._day_start_equity == 0:
            return 0.0
        return ((current_equity - self._day_start_equity) / self._day_start_equity) * 100.0

    def get_drawdown_from_peak_pct(self, current_equity: float) -> float:
        if self._peak_equity is None or self._peak_equity == 0:
            return 0.0
        if current_equity >= self._peak_equity:
            return 0.0
        return ((self._peak_equity - current_equity) / self._peak_equity) * 100.0

    # -- Circuit breakers ------------------------------------------------------

    def check_circuit_breakers(self, current_equity: float) -> dict:
        daily_pnl_pct = self.get_daily_pnl_pct(current_equity)
        drawdown_pct = self.get_drawdown_from_peak_pct(current_equity)

        status = {
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "drawdown_from_peak_pct": round(drawdown_pct, 2),
            "daily_limit_hit": False,
            "absolute_drawdown_hit": False,
            "monthly_alert": False,
            "circuit_breaker_active": self._circuit_breaker_active,
            "can_trade": True,
            "risk_per_trade_pct": self.risk_per_trade_pct,
        }

        if daily_pnl_pct <= -self.daily_loss_limit_pct:
            status["daily_limit_hit"] = True
            status["can_trade"] = False
            self._circuit_breaker_active = True
            logger.warning(
                "CIRCUIT BREAKER: Daily loss %.2f%% exceeds limit %.2f%%",
                daily_pnl_pct, self.daily_loss_limit_pct,
            )

        if drawdown_pct >= self.absolute_max_drawdown_pct:
            status["absolute_drawdown_hit"] = True
            status["can_trade"] = False
            logger.critical(
                "ABSOLUTE DRAWDOWN: %.2f%% from peak -- FULL STOP",
                drawdown_pct,
            )

        if drawdown_pct >= self.monthly_drawdown_alert_pct:
            status["monthly_alert"] = True
            status["risk_per_trade_pct"] = self.risk_per_trade_pct / 2
            logger.warning(
                "Drawdown alert: %.2f%% -- reducing risk to %.1f%% per trade",
                drawdown_pct, self.risk_per_trade_pct / 2,
            )

        if self._circuit_breaker_active:
            status["can_trade"] = False

        return status

    # -- Position sizing -------------------------------------------------------

    def calculate_lot_size(
        self,
        equity: float,
        sl_distance_price: float,
        symbol_info: dict,
        risk_override_pct: float | None = None,
    ) -> float:
        risk_pct = risk_override_pct or self.risk_per_trade_pct
        risk_amount = equity * (risk_pct / 100.0)

        tick_value = symbol_info["trade_tick_value"]
        tick_size = symbol_info["trade_tick_size"]

        if tick_size == 0:
            logger.error("tick_size is 0, cannot calculate lot size")
            return 0.0

        value_per_point = tick_value / tick_size
        risk_per_lot = sl_distance_price * value_per_point

        if risk_per_lot == 0:
            logger.error("risk_per_lot is 0, SL distance may be 0")
            return 0.0

        raw_lots = risk_amount / risk_per_lot

        volume_step = symbol_info["volume_step"]
        lots = max(
            symbol_info["volume_min"],
            round(raw_lots / volume_step) * volume_step,
        )

        lots = max(self.min_lot_size, min(self.max_lot_size, lots))
        lots = max(symbol_info["volume_min"], min(symbol_info["volume_max"], lots))

        logger.info(
            "Position size: equity=$%.2f, risk=%.1f%% ($%.2f), SL_dist=%.2f, lots=%.2f",
            equity, risk_pct, risk_amount, sl_distance_price, lots,
        )
        return round(lots, 2)

    # -- Trade permission checks -----------------------------------------------

    def _get_own_positions(self):
        """Get only positions belonging to this bot (by comment prefix or magic number)."""
        all_positions = mt5.positions_get()
        if all_positions is None:
            return []
        return [
            p for p in all_positions
            if p.magic == self._magic_number
            or (self._comment_prefix and p.comment.startswith(self._comment_prefix))
        ]

    def can_open_trade(self, direction: str, current_equity: float) -> tuple[bool, str]:
        self.update_equity_snapshot(current_equity)
        status = self.check_circuit_breakers(current_equity)

        if not status["can_trade"]:
            return False, f"Circuit breaker active -- daily P&L: {status['daily_pnl_pct']}%"

        positions = self._get_own_positions()

        if len(positions) >= self.max_open_positions:
            return False, f"Max open positions reached ({self.max_open_positions})"

        same_dir_count = sum(
            1 for p in positions
            if (direction == "buy" and p.type == mt5.ORDER_TYPE_BUY)
            or (direction == "sell" and p.type == mt5.ORDER_TYPE_SELL)
        )
        if same_dir_count >= self.max_same_direction:
            return False, f"Max same-direction positions reached ({self.max_same_direction})"

        return True, "OK"

    def get_effective_risk_pct(self, current_equity: float) -> float:
        status = self.check_circuit_breakers(current_equity)
        return status["risk_per_trade_pct"]
