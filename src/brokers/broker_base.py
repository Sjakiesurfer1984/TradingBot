from __future__ import annotations

from src.utilities.logger import setup_logger

logger = setup_logger("BrokerBase")


class BrokerBase:
    """Shared helpers for broker adapters."""

    def _log_io_boundary(self, method_name: str) -> None:
        logger.info("Broker IO | method=%s", method_name)

    @staticmethod
    def _safe_float(value, *, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
