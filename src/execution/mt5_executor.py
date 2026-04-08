"""MT5 order execution -- place, modify, and close trades."""

import logging
from datetime import datetime, timezone

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)


class MT5Executor:
    """Handles all order operations via the MT5 Python API."""

    def __init__(self, symbol: str = "XAUUSDm"):
        self.symbol = symbol

    def _get_filling_mode(self) -> int:
        info = mt5.symbol_info(self.symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC

        filling = info.filling_mode
        if filling & 1:
            return mt5.ORDER_FILLING_FOK
        if filling & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    # -- Market orders ---------------------------------------------------------

    def open_trade(
        self,
        direction: str,
        lots: float,
        sl_price: float,
        tp_price: float,
        comment: str = "HFTBot",
        magic: int = 234567,
    ) -> dict | None:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            logger.error("Cannot get tick for %s", self.symbol)
            return None

        if direction == "buy":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        elif direction == "sell":
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            logger.error("Invalid direction: %s", direction)
            return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": round(sl_price, 2),
            "tp": round(tp_price, 2),
            "deviation": 20,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }

        result = mt5.order_send(request)
        if result is None:
            logger.error("order_send returned None -- MT5 error: %s", mt5.last_error())
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "Order failed -- retcode: %s, comment: %s",
                result.retcode, result.comment,
            )
            return None

        logger.info(
            "ORDER OPENED: %s %s %.2f lots @ %.2f | SL: %.2f | TP: %.2f | Ticket: %s",
            direction.upper(), self.symbol, lots, result.price,
            sl_price, tp_price, result.order,
        )

        return {
            "ticket": result.order,
            "direction": direction,
            "symbol": self.symbol,
            "lots": lots,
            "entry_price": result.price,
            "sl": sl_price,
            "tp": tp_price,
            "time": datetime.now(timezone.utc),
            "comment": comment,
        }

    # -- Close positions -------------------------------------------------------

    def close_trade(self, ticket: int, comment: str = "HFTBot close") -> bool:
        position = mt5.positions_get(ticket=ticket)
        if not position:
            logger.warning("Position %s not found", ticket)
            return False

        pos = position[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            logger.error("Cannot get tick for %s", pos.symbol)
            return False

        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": pos.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error = result.comment if result else mt5.last_error()
            logger.error("Close failed for ticket %s: %s", ticket, error)
            return False

        logger.info("POSITION CLOSED: Ticket %s @ %.2f", ticket, result.price)
        return True

    def close_all(self, comment: str = "HFTBot close all", magic: int = 0) -> int:
        """Close all open positions belonging to this bot (filtered by magic number)."""
        positions = mt5.positions_get()
        if not positions:
            return 0

        own = [p for p in positions if p.magic == magic] if magic else list(positions)
        closed = 0
        for pos in own:
            if self.close_trade(pos.ticket, comment):
                closed += 1
        logger.info("Closed %d/%d positions (magic=%d)", closed, len(own), magic)
        return closed

    # -- Modify SL/TP ----------------------------------------------------------

    def modify_sl_tp(
        self, ticket: int, new_sl: float | None = None, new_tp: float | None = None
    ) -> bool:
        position = mt5.positions_get(ticket=ticket)
        if not position:
            logger.warning("Position %s not found for modification", ticket)
            return False

        pos = position[0]
        sl = round(new_sl, 2) if new_sl is not None else pos.sl
        tp = round(new_tp, 2) if new_tp is not None else pos.tp

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": sl,
            "tp": tp,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error = result.comment if result else mt5.last_error()
            logger.error("Modify failed for ticket %s: %s", ticket, error)
            return False

        logger.info("MODIFIED: Ticket %s -- SL: %.2f, TP: %.2f", ticket, sl, tp)
        return True

    # -- Position queries ------------------------------------------------------

    def get_open_positions(self) -> list[dict]:
        positions = mt5.positions_get()
        if not positions:
            return []

        result = []
        for pos in positions:
            result.append({
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "direction": "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell",
                "lots": pos.volume,
                "entry_price": pos.price_open,
                "current_price": pos.price_current,
                "sl": pos.sl,
                "tp": pos.tp,
                "profit": pos.profit,
                "swap": pos.swap,
                "magic": pos.magic,
                "comment": pos.comment,
                "time": datetime.fromtimestamp(pos.time, tz=timezone.utc),
            })
        return result
