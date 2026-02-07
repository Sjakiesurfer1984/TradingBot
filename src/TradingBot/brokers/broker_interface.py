from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from TradingBot.brokers.account_snapshot import AccountSnapshot
from TradingBot.brokers.broker_base import BrokerBase
from TradingBot.brokers.execution_broker import ExecutionBrokerABC
from TradingBot.brokers.market_data_provider import MarketDataProviderABC


class BrokerABC(BrokerBase, MarketDataProviderABC, ExecutionBrokerABC, ABC):
    """
    Full broker contract used by the Orchestrator.

    This composes:
    - MarketDataProviderABC (market data)
    - ExecutionBrokerABC (execution)
    - plus account-state reads (equity, buying power, positions, open orders)

    BrokerBase is included so all brokers share common utilities and logging.
    """

    # -------------------------
    # Account state (reads)
    # -------------------------

    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_option_buying_power(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_equity(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self) -> List[Dict[str, Any]]:
        raise NotImplementedError


# Transitional alias:
# Keep existing imports working:
# from TradingBot.brokers.broker_interface import BrokerInterface
BrokerInterface = BrokerABC
