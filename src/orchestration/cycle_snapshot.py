from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Tuple

from src.domain.signals import SignalSnapshot
from src.domain.types import AssetQuote, Symbol


class CycleSnapshotABC(ABC):
    @abstractmethod
    def as_of_utc(self) -> datetime:            raise NotImplementedError
    @abstractmethod
    def equity(self) -> float:                  raise NotImplementedError
    @abstractmethod
    def option_buying_power(self) -> float:     raise NotImplementedError
    @abstractmethod
    def positions(self) -> List[Dict[str, Any]]: raise NotImplementedError
    @abstractmethod
    def open_orders(self) -> List[Dict[str, Any]]: raise NotImplementedError
    @abstractmethod
    def asset_quotes(self) -> Mapping[Symbol, AssetQuote]: raise NotImplementedError
    @abstractmethod
    def option_chains(self) -> Mapping[Tuple[Symbol, str], List[Dict[str, Any]]]: raise NotImplementedError
    @abstractmethod
    def signals(self) -> SignalSnapshot:        raise NotImplementedError


@dataclass(frozen=True)
class CycleSnapshot(CycleSnapshotABC):
    _as_of_utc:           datetime
    _equity:              float
    _option_buying_power: float
    _positions:           List[Dict[str, Any]]
    _open_orders:         List[Dict[str, Any]]
    _asset_quotes:        Dict[Symbol, AssetQuote]
    _option_chains:       Dict[Tuple[Symbol, str], List[Dict[str, Any]]]
    _signals:             SignalSnapshot

    def as_of_utc(self) -> datetime:                  return self._as_of_utc
    def equity(self) -> float:                        return float(self._equity)
    def option_buying_power(self) -> float:           return float(self._option_buying_power)
    def positions(self) -> List[Dict[str, Any]]:      return list(self._positions)
    def open_orders(self) -> List[Dict[str, Any]]:    return list(self._open_orders)
    def asset_quotes(self) -> Mapping[Symbol, AssetQuote]: return dict(self._asset_quotes)
    def option_chains(self) -> Mapping[Tuple[Symbol, str], List[Dict[str, Any]]]: return dict(self._option_chains)
    def signals(self) -> SignalSnapshot:              return self._signals

    def with_signals(self, signals: SignalSnapshot) -> "CycleSnapshot":
        return replace(self, _signals=signals)
