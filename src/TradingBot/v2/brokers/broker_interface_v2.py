from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope
from TradingBot.v2.brokers.account_snapshot import AccountSnapshot
logger = setup_logger("Broker Interface")


class BrokerInterfaceV2(ABC):
    """
    V2 broker interface.

    Why this exists
    - V2 is a clean rewrite, so it must not import any V1 broker code.
    - Option C requires that ONLY the orchestrator performs broker IO.
    - A strict interface allows:
        - FakeBrokerV2 for dry-run and unit tests
        - AlpacaBrokerV2 for real connectivity

    Hard rule
    - Implementations must NOT perform network IO in __init__ or __post_init__.
      Construction must be cheap and deterministic.
      Network calls must only happen inside explicit methods.

    Data shape note
    - For now, positions and orders use Dict[str, Any] and List[Dict[str, Any]] because
      broker payloads differ and we are still stabilising V2.
    - Later we will replace these with strongly typed V2 domain models.
    """

    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshot:
        """
        Return a point-in-time snapshot of account state.

        Why this exists
        - Avoids calling /v2/account multiple times per orchestration cycle.
        - Gives orchestrator one object containing the account values it needs.
        """
        raise NotImplementedError

    @abstractmethod
    def get_option_buying_power(self) -> float:
        """
        Return available option buying power as a positive float.

        Why this matters
        - Risk and allocation need a single number that represents usable capital.
        """
        raise NotImplementedError

    @abstractmethod
    def get_equity(self) -> float:
        """
        Return account equity as a positive float.

        Why this matters
        - Equity is used by drawdown rules and reporting.
        """
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> Dict[str, Any]:
        """
        Return current positions.

        Why a dict
        - Positions are typically keyed by symbol for fast lookup.
        """
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Return currently open orders.

        Why a list
        - Open orders are naturally represented as a list of records.
        - Deduplication rules scan this list.
        """
        raise NotImplementedError

    @abstractmethod
    def get_asset_price(self, symbol: str) -> float:
        """
        Return the current market price for an underlying symbol.

        Why this matters
        - Orchestrator fetches prices once per cycle and stores them in RiskContext.
        - Strategies consume prices from RiskContext and never call the broker.
        """
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order: Any) -> Any:
        """
        Submit an order request.

        Why Any for now
        - Order request types will be formalised in v2/domain/orders.py.
        - This keeps the broker usable while we stabilise the architecture.

        Safety rule
        - Only the orchestrator may call submit_order.
        """
        raise NotImplementedError

    def _log_io_boundary(self, method_name: str) -> None:
        """
        Log an explicit IO-boundary marker for broker calls.

        Why this exists
        - Broker methods are the only allowed place for network IO.
        - When debugging hangs, it helps to see the exact transition point
          between pure code and IO calls.

        Notes
        - Implementations may call this at the start of their public methods.
        - This default implementation is optional and does not change behaviour.
        """
        with log_scope("broker_interface.io_boundary", logger, extra=f"method={method_name}"):
            logger.info("Broker IO boundary entered | method=%s", method_name)
