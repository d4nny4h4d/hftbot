"""HFTBot main entry point -- connects MT5, runs BB Reversion strategy on a schedule.

Usage:
    python -m src.main --config config/bot1_london_1pct.yaml --env .env.bot1
"""

import datetime as dt_module
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import dotenv_values

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.alerts.notifier import TelegramNotifier
from src.data.market_feed import MarketFeed
from src.db.database import Database
from src.execution.mt5_executor import MT5Executor
from src.risk.manager import RiskManager
from src.strategy.engine import StrategyEngine

logger = logging.getLogger("hftbot")


class HFTBot:
    """Main application -- wires all components and runs the trading loop."""

    def __init__(self, config_path: str, env_path: str):
        self.config_path = config_path
        self.env_path = env_path
        self._running = False

        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.env = dotenv_values(env_path)
        # Expose MT5_LOGIN to os.environ so RegimeDetector can find the ADX bridge file
        if self.env.get("MT5_LOGIN"):
            os.environ["MT5_LOGIN"] = self.env["MT5_LOGIN"]
        self.check_interval = self.config["general"]["check_interval_seconds"]
        self.bot_name = self.config["general"].get("bot_name", "HFTBot")
        self.bot_id = self.config["general"].get("bot_id", "hftbot")
        self.comment_prefix = self.config["general"].get("comment_prefix", "HF1_")

        # Initialize components
        self.feed = MarketFeed(config_path, env_path)
        self.risk = RiskManager(config_path)
        self.db = Database(self.config["general"]["db_path"])
        self.notifier = TelegramNotifier(env_path, bot_name=self.bot_name)
        self.executor: MT5Executor | None = None
        self.engine: StrategyEngine | None = None
        self.scheduler: BackgroundScheduler | None = None

        # Track for daily summary
        self._day_start_equity: float | None = None
        self._day_trades = 0
        self._day_wins = 0
        self._day_losses = 0
        self._current_date: str | None = None

    def start(self):
        """Connect to MT5 and start the trading loop."""
        self._setup_logging()

        logger.info("=" * 50)
        logger.info("%s starting...", self.bot_name)
        logger.info("Mode: %s", self.env.get("TRADING_MODE", "demo"))
        logger.info("Config: %s", self.config_path)
        logger.info("=" * 50)

        # Connect to MT5
        if not self.feed.connect():
            logger.critical("Failed to connect to MT5 -- exiting")
            return False

        # Create executor with detected symbol
        self.executor = MT5Executor(self.feed.symbol)

        # Create strategy engine
        self.engine = StrategyEngine(
            feed=self.feed,
            executor=self.executor,
            risk_manager=self.risk,
            database=self.db,
            config_path=self.config_path,
        )

        # Get initial account info
        account = self.feed.get_account_info()
        if account:
            self._day_start_equity = account["equity"]
            self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            logger.info("Account equity: $%.2f", account["equity"])
            logger.info("Account leverage: 1:%d", account["leverage"])

        # Send startup alert
        status = self.engine.get_status()
        self.notifier.alert_system_start(
            mode=self.env.get("TRADING_MODE", "demo"),
            equity=account["equity"] if account else 0,
            strategies=status["active_strategies"],
        )

        # Schedule the main trading loop
        self.scheduler = BackgroundScheduler()
        self.scheduler.add_job(
            self._trading_tick,
            "interval",
            seconds=self.check_interval,
            id="trading_tick",
            max_instances=1,
        )

        # Schedule daily summary at 21:00 UTC
        self.scheduler.add_job(
            self._daily_summary,
            "cron",
            hour=21, minute=0,
            timezone="UTC",
            id="daily_summary",
        )

        # Schedule equity snapshots every 5 minutes
        self.scheduler.add_job(
            self._equity_snapshot,
            "interval",
            minutes=5,
            id="equity_snapshot",
        )

        self.scheduler.start()
        self._running = True

        logger.info("Scheduler started -- checking every %ds", self.check_interval)

        # Handle shutdown gracefully
        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)

        # Keep main thread alive
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop("Keyboard interrupt")

        return True

    def stop(self, reason: str = "Manual"):
        """Gracefully shut down the bot."""
        logger.info("Shutting down -- reason: %s", reason)
        self._running = False

        if self.scheduler:
            self.scheduler.shutdown(wait=False)

        self.notifier.alert_system_stop(reason)
        self.feed.disconnect()
        self.db.close()

        logger.info("%s stopped", self.bot_name)

    def _shutdown_handler(self, signum, frame):
        self.stop("Signal received")

    # -- Scheduled tasks -------------------------------------------------------

    def _trading_tick(self):
        """Main trading loop -- called every check_interval_seconds."""
        try:
            if not self.feed.ensure_connected():
                self.notifier.alert_connection_lost()
                return

            # Check for new day
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self._current_date:
                self._new_day(today)

            # Run the strategy engine
            result = self.engine.tick()

            # If a trade was opened, send Telegram alert
            if result and result.get("type") == "trade_opened":
                trade_result = result["result"]
                self.notifier.trade_opened(
                    ticket=trade_result["ticket"],
                    trade={
                        "direction": trade_result["direction"],
                        "entry_price": trade_result["entry_price"],
                        "sl": trade_result["sl"],
                        "tp": trade_result["tp"],
                        "lots": trade_result["lots"],
                        "comment": trade_result.get("comment", self.comment_prefix),
                    },
                    risk_pct=result["risk_pct"],
                    regime=result["regime"],
                )

            # Update live P&L on open positions
            self._update_open_positions()

            # Check for newly closed positions
            self._check_closed_positions()

        except Exception as e:
            logger.exception("Error in trading tick: %s", e)

    def _update_open_positions(self):
        """Update Telegram messages for open positions with current P&L."""
        mt5_positions = self.executor.get_open_positions()
        db_open = self.db.get_open_trades()

        for pos in mt5_positions:
            if not pos.get("comment", "").startswith(self.comment_prefix):
                continue

            db_trade = next(
                (t for t in db_open if t["ticket"] == pos["ticket"]),
                None,
            )
            equity_at_entry = db_trade.get("equity_at_entry", 500) if db_trade else 500
            open_time = db_trade.get("entry_time", "") if db_trade else ""

            self.notifier.trade_updated(
                ticket=pos["ticket"],
                trade={
                    "direction": pos["direction"],
                    "entry_price": pos["entry_price"],
                    "current_price": pos["current_price"],
                    "sl": pos["sl"],
                    "tp": pos["tp"],
                    "lots": pos.get("lots", pos.get("volume", 0)),
                    "profit": pos["profit"],
                    "equity_at_entry": equity_at_entry,
                    "open_time": open_time,
                },
            )

    def _check_closed_positions(self):
        """Detect positions that were closed (by SL/TP) since last check."""
        db_open = self.db.get_open_trades()
        mt5_open_tickets = {
            p["ticket"] for p in self.executor.get_open_positions()
            if p.get("comment", "").startswith(self.comment_prefix)
        }

        for trade in db_open:
            if trade["ticket"] not in mt5_open_tickets:
                import MetaTrader5 as mt5_mod
                from_date = datetime.now(timezone.utc) - dt_module.timedelta(days=7)
                to_date = datetime.now(timezone.utc)
                deals = mt5_mod.history_deals_get(from_date, to_date)

                exit_price = 0.0
                profit_usd = 0.0
                commission = 0.0
                swap = 0.0
                deal_found = False

                if deals:
                    for deal in deals:
                        if deal.position_id == trade["ticket"] and deal.entry == mt5_mod.DEAL_ENTRY_OUT:
                            exit_price = deal.price
                            profit_usd = deal.profit
                            commission = deal.commission
                            swap = deal.swap
                            deal_found = True
                            break

                if not deal_found:
                    logger.warning(
                        "No closing deal found for ticket %s -- skipping close log",
                        trade["ticket"],
                    )
                    continue

                account = self.feed.get_account_info()
                equity = account["equity"] if account else 0

                self.db.log_trade_close(
                    ticket=trade["ticket"],
                    exit_price=exit_price,
                    profit_usd=profit_usd,
                    equity_at_exit=equity,
                    commission=commission,
                    swap=swap,
                )

                if profit_usd >= 0:
                    self._day_wins += 1
                else:
                    self._day_losses += 1
                self._day_trades += 1

                self.notifier.trade_closed(
                    ticket=trade["ticket"],
                    trade={
                        "direction": trade.get("direction", ""),
                        "entry_price": trade.get("entry_price", 0),
                        "exit_price": exit_price,
                        "sl": trade.get("sl", 0),
                        "tp": trade.get("tp", 0),
                        "lots": trade.get("lots", 0),
                        "profit": profit_usd,
                        "equity_at_entry": trade.get("equity_at_entry", 500),
                        "strategy": trade.get("strategy", "BB_Reversion"),
                    },
                )

                profit_pct = (profit_usd / trade.get("equity_at_entry", 500)) * 100 if trade.get("equity_at_entry") else 0
                logger.info(
                    "Position closed: Ticket %s | P&L: $%.2f (%.2f%%)",
                    trade["ticket"], profit_usd, profit_pct,
                )

    def _equity_snapshot(self):
        """Save periodic equity snapshot."""
        try:
            account = self.feed.get_account_info()
            if account:
                own_positions = [
                    p for p in self.executor.get_open_positions()
                    if p.get("comment", "").startswith(self.comment_prefix)
                ]
                self.db.save_equity_snapshot(
                    equity=account["equity"],
                    balance=account["balance"],
                    unrealized_pnl=account["profit"],
                    open_positions=len(own_positions),
                )
        except Exception as e:
            logger.error("Equity snapshot failed: %s", e)

    def _daily_summary(self):
        """Generate and send end-of-day summary."""
        try:
            account = self.feed.get_account_info()
            if not account:
                return

            end_equity = account["equity"]
            start_equity = self._day_start_equity or end_equity
            pnl_usd = end_equity - start_equity
            pnl_pct = (pnl_usd / start_equity * 100) if start_equity > 0 else 0
            win_rate = (self._day_wins / self._day_trades * 100) if self._day_trades > 0 else 0

            max_dd_pct = self.risk.get_drawdown_from_peak_pct(end_equity)

            self.db.save_daily_summary(
                date=self._current_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                start_equity=start_equity,
                end_equity=end_equity,
                trades_count=self._day_trades,
                wins=self._day_wins,
                losses=self._day_losses,
                max_drawdown_pct=max_dd_pct,
                circuit_breaker_hit=self.risk._circuit_breaker_active,
            )

            self.notifier.alert_daily_summary({
                "date": self._current_date,
                "start_equity": start_equity,
                "end_equity": end_equity,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "trades": self._day_trades,
                "wins": self._day_wins,
                "losses": self._day_losses,
                "win_rate_pct": win_rate,
                "max_drawdown_pct": max_dd_pct,
            })

            logger.info(
                "DAILY SUMMARY: %s | P&L: %+.2f%% ($%+.2f) | Trades: %d (W:%d L:%d)",
                self._current_date, pnl_pct, pnl_usd,
                self._day_trades, self._day_wins, self._day_losses,
            )

        except Exception as e:
            logger.error("Daily summary failed: %s", e)

    def _new_day(self, today: str):
        """Reset daily counters for new trading day."""
        account = self.feed.get_account_info()
        if account:
            self._day_start_equity = account["equity"]

        self._current_date = today
        self._day_trades = 0
        self._day_wins = 0
        self._day_losses = 0
        logger.info("New trading day: %s", today)

    # -- Logging setup ---------------------------------------------------------

    def _setup_logging(self):
        log_level = self.config["general"].get("log_level", "INFO")
        log_file = f"data/{self.bot_id}.log"
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=getattr(logging, log_level),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(log_file, encoding="utf-8"),
            ],
        )


# -- Entry point ---------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HFTBot -- Automated XAU/USD Trading")
    parser.add_argument("--config", required=True, help="Path to config YAML file")
    parser.add_argument("--env", required=True, help="Path to .env file")
    args = parser.parse_args()

    bot = HFTBot(config_path=args.config, env_path=args.env)
    bot.start()
