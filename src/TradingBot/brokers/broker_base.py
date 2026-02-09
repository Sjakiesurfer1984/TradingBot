# src/TradingBot/v2/brokers/broker_base.py
from __future__ import annotations

from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

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


# The entire broker building process works to an Abstract Factory with Registry. 
# main() main is a conductor, not a mechanic.It needs a Broker object that satisfies the BrokerABC interface. How, does not matter
# BrokerFactory: Coordinate broker creation, nothing else.“Who should build this, and please do it.”
# BrokerRegistry: Lookup table, Mapping of names -> builders, single source of truth. Registry answers: “Yes, that broker exists. Here is who can build it.”
# BrokerBuilderABC: Define HOW a broker must be constructed. “If you want to be a broker builder, you MUST be able to turn AppConfig into a BrokerABC.”
# AlpacaBrokerBuilder: All Alpaca-specific construction logic. Retuns AlpacaBroker, that conforms to the BrokerABC. 
# main
#   └── BrokerFactory
#         └── BrokerRegistry
#               └── BrokerBuilderABC
#                     └── AlpacaBrokerBuilder
#                           └── AlpacaBroker
