from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BrokerInterfaceV2(ABC):
    """
    V2 broker interface.

    Why this exists
    - V2 is a clean rewrite, so it must not import V1 broker code.
    - The orchestrator is the only layer allowed to talk to the broker.
    - A strict interface makes it easy to swap implementations:
        - FakeBrokerV2 for dry-run and tests
        - AlpacaBrokerV2 for real trading connectivity

    Design rule
    - Implementations must not perform network IO in __init__ or __post_init__.
      Constructors must be cheap and side-effect free.
      Network calls must happen inside explicit methods only.

    Data shape notes
    - Positions and orders are left as Dict[str, Any] and List[Dict[str, Any]] for now because
      broker payloads differ between providers.
    - We will introduce typed V2 domain models for positions and orders later, once the system
      flow is stable.
    """

    @abstractmethod # an abstract method decorator is used to indicate that this method must be overridden in subclasses 
    def get_option_buying_power(self) -> float:
        """
        Return available option buying power.

        Why this matters
        - Risk and allocation need a single source of truth for available capital.
        """
        raise NotImplementedError

    @abstractmethod
    def get_equity(self) -> float:
        """
        Return account equity.

        Why this matters
        - Equity is used for drawdown rules, exposure limits, and reporting.
        """
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> Dict[str, Any]:
        """
        Return current positions.

        Why a dict
        - Fast lookup by symbol is usually needed.
        """
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Return currently open orders.

        Why a list
        - Open orders are typically processed as a collection of records.
        """
        raise NotImplementedError

    @abstractmethod
    def get_asset_price(self, symbol: str) -> float:
        """
        Return the current market price for an underlying symbol.

        Why this matters
        - Orchestrator uses this to populate RiskContext.prices.
        - Strategies consume prices from RiskContext rather than calling the broker directly.
        """
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order: Any) -> Any:
        """
        Submit an order request.

        Why Any
        - Order request types will be formalised in v2/domain/orders.py.
        - This keeps the interface usable while V2 is being built.

        Important safety rule
        - Only the orchestrator is allowed to call submit_order.
        - Strategies and risk engine must never call it.
        """
        raise NotImplementedError
