from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Optional

from TradingBot.v2.logger import setup_logger

logger = setup_logger("LoggingUtils")


@contextmanager
def log_scope(
    name: str,
    module_logger,
    extra: Optional[str] = None,
) -> Iterator[None]:
    """
    Log a start and end line with duration.

    Why this exists
    - Consistent visibility without repeating boilerplate everywhere.
    - Duration helps pinpoint where things slow or stall.
    """
    msg_start: str = f"Start {name}"
    if extra:
        msg_start = f"{msg_start} | {extra}"

    module_logger.info(msg_start)
    t0: float = time.monotonic()
    try:
        yield
    finally:
        elapsed: float = time.monotonic() - t0
        msg_end: str = f"End {name} | elapsed={elapsed:.3f}s"
        module_logger.info(msg_end)
