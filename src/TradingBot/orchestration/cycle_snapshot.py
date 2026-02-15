from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Tuple

from TradingBot.domain.signals import SignalSnapshot
from TradingBot.domain.types import AssetQuote, Symbol
from TradingBot.risk.price_policy import PriceSelectionPolicy


class CycleSnapshotABC(ABC):
    """
    Per cycle snapshot contract.

    Contract
    - Immutable view of what the rest of the cycle operates on.
    - Contains per cycle signals computed after IO is completed.
    - Carries a price selection policy (even if not used yet).
    """

    @abstractmethod
    def as_of_utc(self) -> datetime:
        raise NotImplementedError

    @abstractmethod
    def option_buying_power(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def equity(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def positions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def open_orders(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def asset_quotes(self) -> Mapping[Symbol, AssetQuote]:
        raise NotImplementedError

    @abstractmethod
    def option_chains(self) -> Mapping[Tuple[Symbol, str], List[Dict[str, Any]]]:
        raise NotImplementedError

    @abstractmethod
    def signals(self) -> SignalSnapshot:
        raise NotImplementedError

    @abstractmethod
    def with_signals(self, signals: SignalSnapshot) -> "CycleSnapshotABC":
        raise NotImplementedError

    @abstractmethod
    def price_policy(self) -> PriceSelectionPolicy:
        raise NotImplementedError


@dataclass(frozen=True)
class CycleSnapshot(CycleSnapshotABC):
    _as_of_utc: datetime
    _option_buying_power: float
    _equity: float
    _positions: List[Dict[str, Any]]
    _open_orders: List[Dict[str, Any]]
    _asset_quotes: Dict[Symbol, AssetQuote]
    _option_chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]]
    _signals: SignalSnapshot
    _price_policy: PriceSelectionPolicy

    def as_of_utc(self) -> datetime:
        return self._as_of_utc

    def option_buying_power(self) -> float:
        return float(self._option_buying_power)

    def equity(self) -> float:
        return float(self._equity)

    def positions(self) -> List[Dict[str, Any]]:
        return list(self._positions)

    def open_orders(self) -> List[Dict[str, Any]]:
        return list(self._open_orders)

    def asset_quotes(self) -> Mapping[Symbol, AssetQuote]:
        return dict(self._asset_quotes)

    def option_chains(self) -> Mapping[Tuple[Symbol, str], List[Dict[str, Any]]]:
        return dict(self._option_chains)

    def signals(self) -> SignalSnapshot:
        return self._signals

    def with_signals(self, signals: SignalSnapshot) -> "CycleSnapshot":
        return replace(self, _signals=signals)

    def price_policy(self) -> PriceSelectionPolicy:
        return self._price_policy
