# strategy_interface.py
from abc import ABC, abstractmethod
from TradingBot.bot_state import BotState


class Strategy(ABC):
    @abstractmethod
    def execute(self, state: "BotState") -> None:
        """Executes the trading strategy."""
        pass
