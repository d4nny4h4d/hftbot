"""Telegram alert system -- live-updating trade messages, daily summaries, circuit breakers.

Design:
- On trade OPEN: sends a message showing entry, SL, TP, open P&L
- While trade is LIVE: edits the same message to update P&L
- On trade CLOSE: edits the same message to show final summary
- Bot name prefix on all messages so multiple bots share one chat
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends and edits formatted alerts to Telegram with bot name prefix."""

    def __init__(self, env_path: str = ".env", bot_name: str = "HFTBot"):
        env = dotenv_values(env_path)
        self.token = env.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = env.get("TELEGRAM_CHAT_ID", "")
        self.bot_name = bot_name
        self._bot = None
        self._loop = None
        self._enabled = bool(self.token and self.chat_id)

        # Persist message IDs so edits survive bot restarts
        self._msg_file = Path(f"data/.telegram_messages_{bot_name}.json")
        self._trade_messages: dict[int, int] = self._load_messages()

        if not self._enabled:
            logger.warning("Telegram not configured -- alerts disabled")

    def _load_messages(self) -> dict[int, int]:
        """Load ticket -> message_id mapping from disk."""
        if self._msg_file.exists():
            try:
                data = json.loads(self._msg_file.read_text())
                return {int(k): int(v) for k, v in data.items()}
            except Exception:
                return {}
        return {}

    def _save_messages(self):
        """Persist ticket -> message_id mapping to disk."""
        try:
            self._msg_file.parent.mkdir(parents=True, exist_ok=True)
            self._msg_file.write_text(json.dumps(
                {str(k): v for k, v in self._trade_messages.items()}
            ))
        except Exception as exc:
            logger.warning("Failed to save message IDs: %s", exc)

    def _get_bot_sync(self):
        """Get or create a Bot instance bound to our persistent event loop."""
        if self._bot is None and self._enabled:
            from telegram import Bot
            self._bot = Bot(token=self.token)
        return self._bot

    def _run_async(self, coro):
        """Run an async coroutine using a persistent event loop.

        Reuses the same loop for the Bot's lifetime so the internal httpx
        client never references a closed loop.
        """
        try:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                # Force a fresh Bot bound to the new loop
                self._bot = None
            asyncio.set_event_loop(self._loop)
            return self._loop.run_until_complete(coro)
        except Exception as e:
            logger.error("Async execution failed: %s", e)
            # If the loop broke, reset so next call creates a fresh one
            self._loop = None
            self._bot = None
            return None

    def _send_sync(self, text: str) -> int | None:
        """Send a message. Returns the message_id for later editing."""
        if not self._enabled:
            return None
        bot = self._get_bot_sync()
        if not bot:
            return None

        async def _do():
            msg = await bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
            )
            return msg.message_id

        try:
            return self._run_async(_do())
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return None

    def _edit_sync(self, message_id: int, text: str) -> bool:
        """Edit an existing message by ID."""
        if not self._enabled or not message_id:
            return False
        bot = self._get_bot_sync()
        if not bot:
            return False

        async def _do():
            await bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
            )
            return True

        try:
            return self._run_async(_do()) or False
        except Exception as e:
            if "not modified" not in str(e).lower():
                logger.error("Telegram edit failed: %s", e)
            return False

    # -- Trade lifecycle messages -----------------------------------------------

    def trade_opened(self, ticket: int, trade: dict, risk_pct: float, regime: str):
        direction = trade["direction"].upper()
        emoji = "\U0001f7e2" if trade["direction"] == "buy" else "\U0001f534"

        msg = (
            f"{emoji} <b>[{self.bot_name}] TRADE OPEN -- {direction} XAUUSDm</b>\n"
            f"\n"
            f"Entry:  <code>{trade['entry_price']:.2f}</code>\n"
            f"SL:     <code>{trade['sl']:.2f}</code>\n"
            f"TP:     <code>{trade['tp']:.2f}</code>\n"
            f"Lots:   <code>{trade['lots']:.2f}</code>\n"
            f"Risk:   <code>{risk_pct:.1f}%</code>\n"
            f"\n"
            f"P&L:    <code>$0.00 (0.00%)</code>\n"
            f"Status: <b>OPEN</b>\n"
            f"\n"
            f"Strategy: {trade.get('comment', 'BB_Reversion')}\n"
            f"Regime: {regime}\n"
            f"Opened: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )

        message_id = self._send_sync(msg)
        if message_id:
            self._trade_messages[ticket] = message_id
            self._save_messages()

    def trade_updated(self, ticket: int, trade: dict):
        direction = trade["direction"].upper()
        emoji = "\U0001f7e2" if trade["direction"] == "buy" else "\U0001f534"

        profit = trade.get("profit", 0)
        entry = trade.get("entry_price", 0)
        current = trade.get("current_price", 0)
        equity_at_entry = trade.get("equity_at_entry", 500)
        profit_pct = (profit / equity_at_entry * 100) if equity_at_entry else 0

        pnl_emoji = "\U0001f4b0" if profit >= 0 else "\U0001f4c9"

        msg = (
            f"{emoji} <b>[{self.bot_name}] TRADE OPEN -- {direction} XAUUSDm</b>\n"
            f"\n"
            f"Entry:    <code>{entry:.2f}</code>\n"
            f"Current:  <code>{current:.2f}</code>\n"
            f"SL:       <code>{trade.get('sl', 0):.2f}</code>\n"
            f"TP:       <code>{trade.get('tp', 0):.2f}</code>\n"
            f"Lots:     <code>{trade.get('lots', 0):.2f}</code>\n"
            f"\n"
            f"{pnl_emoji} P&L:  <code>${profit:+.2f} ({profit_pct:+.2f}%)</code>\n"
            f"Status: <b>OPEN</b>\n"
            f"\n"
            f"Opened: {trade.get('open_time', '')}"
        )

        message_id = self._trade_messages.get(ticket)
        if message_id:
            self._edit_sync(message_id, msg)
        else:
            new_id = self._send_sync(msg)
            if new_id:
                self._trade_messages[ticket] = new_id
                self._save_messages()

    def trade_closed(self, ticket: int, trade: dict):
        message_id = self._trade_messages.get(ticket)

        direction = trade.get("direction", "").upper()
        entry = trade.get("entry_price", 0)
        exit_price = trade.get("exit_price", 0)
        profit = trade.get("profit", 0)
        equity_at_entry = trade.get("equity_at_entry", 500)
        profit_pct = (profit / equity_at_entry * 100) if equity_at_entry else 0
        lots = trade.get("lots", 0)

        result_emoji = "\u2705" if profit >= 0 else "\u274c"
        result_text = "WIN" if profit >= 0 else "LOSS"

        msg = (
            f"{result_emoji} <b>[{self.bot_name}] TRADE CLOSED -- {direction} XAUUSDm</b>\n"
            f"\n"
            f"Entry:  <code>{entry:.2f}</code>\n"
            f"Exit:   <code>{exit_price:.2f}</code>\n"
            f"Lots:   <code>{lots:.2f}</code>\n"
            f"\n"
            f"P&L:    <code>${profit:+.2f} ({profit_pct:+.2f}%)</code>\n"
            f"Result: <b>{result_text}</b>\n"
            f"Status: <b>CLOSED</b>\n"
            f"\n"
            f"Strategy: {trade.get('strategy', 'BB_Reversion')}\n"
            f"Closed: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )

        if message_id:
            self._edit_sync(message_id, msg)
            self._trade_messages.pop(ticket, None)
            self._save_messages()
        else:
            self._send_sync(msg)

    # -- System alerts ----------------------------------------------------------

    def alert_daily_summary(self, stats: dict):
        pnl_emoji = "\U0001f4c8" if stats.get("pnl_pct", 0) >= 0 else "\U0001f4c9"
        msg = (
            f"{pnl_emoji} <b>[{self.bot_name}] DAILY SUMMARY</b>\n"
            f"Date: {stats.get('date', 'N/A')}\n"
            f"{'='*25}\n"
            f"Start Equity: <code>${stats.get('start_equity', 0):.2f}</code>\n"
            f"End Equity: <code>${stats.get('end_equity', 0):.2f}</code>\n"
            f"Day P&L: <code>{stats.get('pnl_pct', 0):+.2f}%</code> "
            f"(<code>${stats.get('pnl_usd', 0):+.2f}</code>)\n"
            f"Trades: {stats.get('trades', 0)} "
            f"(W: {stats.get('wins', 0)} / L: {stats.get('losses', 0)})\n"
            f"Win Rate: <code>{stats.get('win_rate_pct', 0):.1f}%</code>\n"
            f"Max DD: <code>{stats.get('max_drawdown_pct', 0):.2f}%</code>"
        )
        self._send_sync(msg)

    def alert_circuit_breaker(self, reason: str, daily_pnl_pct: float):
        msg = (
            f"\U000026a0\U0000fe0f <b>[{self.bot_name}] CIRCUIT BREAKER TRIGGERED</b>\n"
            f"Reason: {reason}\n"
            f"Daily P&L: <code>{daily_pnl_pct:+.2f}%</code>\n"
            f"<b>All trading halted until next session</b>"
        )
        self._send_sync(msg)

    def alert_system_start(self, mode: str, equity: float, strategies: list[str]):
        msg = (
            f"\U0001f680 <b>[{self.bot_name}] Started</b>\n"
            f"Mode: <b>{mode.upper()}</b>\n"
            f"Equity: <code>${equity:.2f}</code>\n"
            f"Strategies: {', '.join(strategies)}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send_sync(msg)

    def alert_system_stop(self, reason: str = "Manual"):
        msg = (
            f"\U0001f6d1 <b>[{self.bot_name}] Stopped</b>\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send_sync(msg)

    def alert_connection_lost(self):
        msg = (
            f"\U000026a0\U0000fe0f <b>[{self.bot_name}] MT5 CONNECTION LOST</b>\n"
            f"Attempting reconnection...\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send_sync(msg)
