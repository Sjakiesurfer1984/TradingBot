# risk_manager.py

from dataclasses import dataclass
import logging
from TradingBot.logger import setup_logger  # absolute import from your package

logger = setup_logger("RiskManager")


@dataclass
class RiskManager:
    """
    RiskManager checks if the potential loss of a trade falls within an acceptable limit.

    Attributes:
        max_risk (float): The maximum allowable risk (loss) for a trade.
    """
    max_risk: float

    def check_risk(self, potential_loss: float) -> bool:
        """
        Check if the potential loss is within the acceptable risk threshold.

        Args:
            potential_loss (float): The estimated loss for the trade.

        Returns:
            bool: True if the potential loss is less than or equal to max_risk, False otherwise.
        """
        logger.debug(
            f"Checking risk: potential_loss={potential_loss}, max_risk={self.max_risk}")
        result = potential_loss <= self.max_risk
        logger.debug(f"Risk check result: {result}")
        return result
