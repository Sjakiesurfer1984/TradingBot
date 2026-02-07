# src/TradingBot/v2/brokers/broker_base.py
from __future__ import annotations

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

logger = setup_logger("BrokerBase")


class BrokerBase:
    """
    Shared runtime helpers for broker adapters.

    Important
    - This is a real class (runtime), not a Protocol.
    - Put shared helper implementations here, not inside Protocols.
    """

    def _log_io_boundary(self, method_name: str) -> None:
        """
        Log an explicit IO-boundary marker for broker calls.

        Why this exists
        - Broker methods are the only allowed place for network IO.
        - When debugging hangs, it helps to see the exact transition point
          between pure code and IO calls.
        """
        with log_scope("broker.io_boundary", logger, extra=f"method={method_name}"):
            logger.info("Broker IO boundary entered | method=%s", method_name)
