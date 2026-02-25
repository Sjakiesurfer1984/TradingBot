from __future__ import annotations

from typing import Any, Callable, Mapping, Type, TypeVar

from src.utilities.logger import setup_logger

logger = setup_logger("BrokerBase")

T = TypeVar("T")
R = TypeVar("R")


class BrokerBase:
    """
    Mixin for broker adapters.

    This is NOT a broker implementation.
    It only provides shared helper methods.

    Critical rule:
    - This file must NEVER import BrokerBase from itself.
    """

    def _log_io_boundary(self, method_name: str) -> None:
        logger.info("Broker IO | method=%s", method_name)

    @staticmethod
    def _safe_float(value: Any, *, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _dispatch_by_type(
        value: T,
        handlers: Mapping[Type[Any], Callable[[T], R]],
        *,
        error_prefix: str,
    ) -> R:
        """
        Registry-based dispatch to avoid long isinstance ladders.
        """
        for cls, fn in handlers.items():
            if isinstance(value, cls):
                return fn(value)
        raise TypeError(f"{error_prefix}: {type(value).__name__}")