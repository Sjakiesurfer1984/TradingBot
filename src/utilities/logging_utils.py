from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Optional

from src.utilities.logger import setup_logger

logger = setup_logger("LoggingUtils")


@contextmanager
def log_scope(name: str, module_logger, extra: Optional[str] = None) -> Iterator[None]:
    msg = f"Start {name}" + (f" | {extra}" if extra else "")
    module_logger.info(msg)
    t0 = time.monotonic()
    try:
        yield
    finally:
        module_logger.info(f"End {name} | elapsed={time.monotonic() - t0:.3f}s")
