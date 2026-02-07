from __future__ import annotations

from datetime import datetime

from TradingBot.domain.types import IntentId, StrategyId, Symbol
from TradingBot.utilities.logger import setup_logger
logger = setup_logger("IDs")


def make_intent_id(
    *,
    strategy_id: StrategyId,
    underlying: Symbol,
    as_of_utc: datetime,
) -> IntentId:
    """
    Build a deterministic, traceable intent identifier.

    Why this exists
    - Prevents every strategy inventing its own ID format.
    - Ensures intent IDs are consistent across the entire system.
    - Makes logs, audits, and tests predictable.

    Format
    - {strategy_id}:{underlying}:{YYYYMMDDTHHMMSSZ}

    Important
    - The timestamp comes from RiskContext.as_of_utc.
    - Strategies must not call datetime.now() themselves.
    """
    timestamp: str = as_of_utc.strftime("%Y%m%dT%H%M%SZ")
    return IntentId(f"{strategy_id}:{underlying}:{timestamp}")
