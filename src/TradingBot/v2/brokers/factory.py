from TradingBot.v2.brokers.alpaca_factory import build_alpaca_broker

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope
from TradingBot.v2.brokers.broker_interface import BrokerInterface

logger = setup_logger("Broker Factory")


def build_broker(name: str) -> BrokerInterface:
    """
    Construct a broker adapter based on the provided name.

    Why this exists
    - Centralises broker selection logic.
    - Prevents the orchestrator or main entrypoint from hard-coding broker types.
    - Makes it easy to switch between fake and real brokers via configuration.
    """
    with log_scope("factory.build_broker", logger, extra=f"raw_name={name}"):
        name_norm = (name or "fake").strip().lower()
        logger.info("Normalised broker name | name_norm=%s", name_norm)

        if name_norm == "alpaca":
            logger.info("Selected Alpaca broker")
            broker = build_alpaca_broker()
            logger.info("Alpaca broker constructed | type=%s", type(broker).__name__)
            return broker
        
    raise ValueError(f"Unsupported broker name: {name!r}")


