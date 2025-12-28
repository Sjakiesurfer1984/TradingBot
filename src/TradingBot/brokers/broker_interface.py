from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Dict, List, Literal, Tuple

import pandas as pd

from TradingBot.domain.orders import MultiLegLimitOrder

OptionType = Literal["call", "put"]
AccountInfo = Dict[str, Any]
Position = Dict[str, Any]
Positions = List[Position]


class BrokerInterface(ABC):
    """
    Broker interface that hides third party SDK details from strategies.

    Strategies must only depend on this interface.
    Concrete brokers translate internal domain models into SDK specific requests.
    """

    @abstractmethod
    def get_asset_price(self, symbol: str) -> float:
        """
        Return the latest tradable price for the underlying symbol.
        """
        raise NotImplementedError

    @abstractmethod
    def get_account_info(self) -> AccountInfo:
        """
        Return broker account information in a normalised dict.

        Recommended keys to include:
        - cash
        - equity
        - buying_power
        - options_buying_power (if available)
        """
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> Positions:
        """
        Return current positions in a broker agnostic list of dicts.

        Recommended keys to include:
        - symbol
        - qty
        - avg_entry_price
        - market_value
        - unrealised_pl
        """
        raise NotImplementedError

    @abstractmethod
    def get_option_buying_power(self) -> float:
        """
        Return available buying power suitable for options, as a float.
        """
        raise NotImplementedError

    @abstractmethod
    def get_option_chain(
        self,
        symbol: str,
        tipo: OptionType,
        strike: Tuple[float, float],
        expiration: Tuple[date, date],
    ) -> pd.DataFrame:
        """
        Return an option chain filtered by strike and expiration bounds.

        The returned DataFrame should include (at minimum):
        - option_symbol: str
        - strike: float
        - expiration_date: date or ISO string
        - bid_price: float
        - ask_price: float
        - delta: float
        - implied_volatility: float (decimal, e.g. 0.55)
        - open_interest: int
        - volume: int
        """
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order_request: MultiLegLimitOrder) -> Any:
        """
        Submit a broker agnostic order request.

        The broker adaptor is responsible for translating MultiLegLimitOrder
        to the broker SDK request type and then submitting it.

        Returns:
            Any: Broker specific response object or ID.
        """
        raise NotImplementedError
