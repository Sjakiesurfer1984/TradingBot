from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Protocol

from TradingBot.v2.brokers.account_snapshot import AccountSnapshot
from TradingBot.v2.brokers.execution_broker import ExecutionBroker
from TradingBot.v2.brokers.market_data_provider import MarketDataProvider
from TradingBot.v2.domain.types import AssetQuote


class BrokerInterface(MarketDataProvider, ExecutionBroker, Protocol):
    """
    Broker contract.

    One object must support both:
    - market data reads
    - execution (order submission)

    Orchestrator is the only place allowed to call broker IO methods.
    """

    def get_account_snapshot(self) -> AccountSnapshot:
        ...

    def get_positions(self) -> List[Dict[str, Any]]:
        ...

    def get_open_orders(self) -> List[Dict[str, Any]]:
        ...

    def get_asset_quote(self, symbol: str) -> AssetQuote:
        ...

    def get_option_chain(
        self,
        underlying: str,
        *,
        include_calls: bool = True,
        include_puts: bool = False,
        feed: str = "indicative",
        max_age_seconds: int = 5,
        limit: int = 0,
        strike_price_gte: Optional[float] = None,
        strike_price_lte: Optional[float] = None,
        expiration_date: Optional[date] = None,
        expiration_date_gte: Optional[date] = None,
        expiration_date_lte: Optional[date] = None,
        root_symbol: Optional[str] = None,
        updated_since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        ...

    def submit_order(self, order: Any) -> Any:
        ...








# from __future__ import annotations

# from abc import ABC, abstractmethod
# from typing import Any, Dict, List, Optional, Protocol
# from datetime import date, datetime
# from TradingBot.v2.domain.types import AssetQuote
# from TradingBot.v2.logger import setup_logger
# from TradingBot.v2.logging_utils import log_scope
# from TradingBot.v2.brokers.account_snapshot import AccountSnapshot
# from TradingBot.v2.brokers.execution_broker import ExecutionBroker
# from TradingBot.v2.brokers.market_data_provider import MarketDataProvider

# logger = setup_logger("Broker Interface")

# # ------------------------------------------------------------------------------------------
# # BrokerInterface, abstract base class
# # Defines the interface that all brokers must implement.
# # That is, AlpacaBrokerV2, FakeBrokerV2, etc. must all implement this interface.
# # meaning they must implement all the abstract methods defined here as concrete methods.
# # ------------------------------------------------------------------------------------------

# class BrokerInterface(MarketDataProvider, ExecutionBroker, Protocol):
#     """
#     A broker is both:
#     - a MarketDataProvider (read-only market/account data)
#     - an ExecutionBroker (order submission and execution actions)

#     Option B depends on this being true at the type level, not only at runtime.
#     """
#     pass

# # ------------------------------------------------------------------------------------------
# # get account snapshot
# # ------------------------------------------------------------------------------------------
#     @abstractmethod
#     def get_account_snapshot(self) -> AccountSnapshot:
#         """
#         Return a point-in-time snapshot of account state.

#         Why this exists
#         - Avoids calling /v2/account multiple times per orchestration cycle.
#         - Gives orchestrator one object containing the account values it needs.
#         """
#         raise NotImplementedError
# # ------------------------------------------------------------------------------------------
# # get option buying power, equity, positions, open orders, asset quote, option chain, submit order
# # ------------------------------------------------------------------------------------------
#     @abstractmethod
#     def get_option_buying_power(self) -> float:
#         """
#         Return available option buying power as a positive float.

#         Why this matters
#         - Risk and allocation need a single number that represents usable capital.
#         """
#         raise NotImplementedError

#     @abstractmethod
#     def get_equity(self) -> float:
#         """
#         Return account equity as a positive float.

#         Why this matters
#         - Equity is used by drawdown rules and reporting.
#         """
#         raise NotImplementedError

#     @abstractmethod
#     def get_positions(self) -> List[Dict[str, Any]]:
#         """
#         Return current positions.

#         Why a list
#         - Broker payloads are naturally a list of records.
#         - Risk rules can scan it consistently.
#         """
#         raise NotImplementedError

#     @abstractmethod
#     def get_open_orders(self) -> List[Dict[str, Any]]:
#         """
#         Return currently open orders.

#         Why a list
#         - Open orders are naturally represented as a list of records.
#         - Deduplication rules scan this list.
#         """
#         raise NotImplementedError

#     @abstractmethod
#     def get_asset_quote(self, symbol: str) -> AssetQuote:
#         """
#         Return the latest bid/ask quote for the given symbol.

#         Why this matters
#         - Orchestrator fetches prices once per cycle and stores them in RiskContext.
#         - Strategies consume prices from RiskContext and never call the broker.
#         """
#         raise NotImplementedError  # could also use "pass" instead

#     @abstractmethod
#     def get_option_chain(
#         self,
#         underlying: str,
#         *,
#         include_calls: bool = True,
#         include_puts: bool = False,
#         feed: str = "indicative",
#         max_age_seconds: int = 5, # 5 seconds old Option data is no longer acceptable. This is to ensure we get fresh data. Noting that for testing purposes, we are passing in a default of 3 days to bypass the stale data checker  
#         limit: int = 0,
#         strike_price_gte: Optional[float] = None,
#         strike_price_lte: Optional[float] = None,
#         expiration_date: Optional[date] = None,
#         expiration_date_gte: Optional[date] = None,
#         expiration_date_lte: Optional[date] = None,
#         root_symbol: Optional[str] = None,
#         updated_since: Optional[datetime] = None,
#     ) -> List[Dict[str, Any]]:
#         """
#         Fetch the option chain snapshot for a single underlying.

#         Contract
#         - Broker IO only.
#         - One underlying in, flat list of contracts out.
#         - No strategy logic, no orchestration logic.

#         Notes
#         - Implementations should log whether data is delayed (eg indicative feed).
#         - Implementations may stamp metadata such as _feed, _newest_ts, _is_stale.
#         """
#         raise NotImplementedError

#     @abstractmethod
#     def submit_order(self, order: Any) -> Any:
#         """
#         Submit an order request.

#         Why Any for now
#         - Order request types will be formalised in v2/domain/orders.py.
#         - This keeps the broker usable while we stabilise the architecture.

#         Safety rule
#         - Only the orchestrator may call submit_order.
#         """
#         raise NotImplementedError
    

#     def _log_io_boundary(self, method_name: str) -> None:
#         """
#         Log an explicit IO-boundary marker for broker calls.

#         Why this exists
#         - Broker methods are the only allowed place for network IO.
#         - When debugging hangs, it helps to see the exact transition point
#           between pure code and IO calls.

#         Notes
#         - Implementations may call this at the start of their public methods.
#         - This default implementation is optional and does not change behaviour.
#         """
#         with log_scope("broker_interface.io_boundary", logger, extra=f"method={method_name}"):
#             logger.info("Broker IO boundary entered | method=%s", method_name)
