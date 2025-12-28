# TradingBot/bot_state.py

from dataclasses import dataclass, field
from typing import Dict, List
# Absolute import from the TradingBot package
from TradingBot.logger import setup_logger

logger = setup_logger("BotState")


@dataclass
class BotState:
    """
    BotState holds the current state of the bot, including active orders,
    positions, and available cash.

    Attributes:
        active_orders (List[dict]): A list of active orders.
        positions (Dict[str, dict]): A dictionary mapping ticker symbols to their position details.
        cash_available (float): The current available cash for trading.
    """
    active_orders: List[dict] = field(default_factory=list)
    positions: Dict[str, dict] = field(default_factory=dict)
    cash_available: float = 0.0

    def refresh_account_info(self, broker) -> None:
        """
        Refresh account information by querying the broker. Updates the cash_available attribute.

        Args:
            broker: An object implementing the broker interface, which provides a get_account_info() method.
        """
        logger.debug("Refreshing account info in BotState.")
        account_info = broker.get_account_info()
        logger.debug(f"Account info: {account_info}")
        raw_cash = account_info.get("cash", 0.0)
        try:
            self.cash_available = float(raw_cash)
        except Exception as e:
            logger.error(
                f"Error converting cash value {raw_cash} to float: {e}")
            self.cash_available = 0.0

    def refresh_positions(self, broker) -> None:
        """
        Refresh positions by querying the broker. Updates the positions attribute.

        Args:
            broker: An object implementing the broker interface, which provides a get_positions() method.
        """
        logger.debug("Refreshing positions in BotState.")
        positions = broker.get_positions()
        self.positions = {pos.get("symbol", "unknown")
                                  : pos for pos in positions}

    def update_orders(self, order: dict) -> None:
        """
        Update the list of active orders by appending a new order.

        Args:
            order (dict): A dictionary containing order details.
        """
        logger.debug(f"Updating active orders with: {order}")
        self.active_orders.append(order)
