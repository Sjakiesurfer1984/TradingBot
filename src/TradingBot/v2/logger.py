# src/TradingBot/v2/logger.py
#
# Central logging configuration for TradingBot V2.
#
# Why this exists
# - Ensures consistent formatting across all components.
# - Avoids ad-hoc print statements.
# - Allows future redirection to files, JSON logs, or external systems
#   without touching business logic.

from __future__ import annotations

import logging
from typing import Optional


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Create or retrieve a configured logger for V2 components.

    Design intent
    - One logger per module, identified by name.
    - All loggers share the same formatting and handlers.
    - Calling this function multiple times with the same name
      returns the same logger instance.

    Parameters
    - name:
        Logical name of the logger (usually __name__ or a subsystem name).
    - level:
        Logging level (INFO by default).

    Returns
    - A configured logging.Logger instance.
    """
    logger: logging.Logger = logging.getLogger(name)

    # Avoid attaching multiple handlers if setup_logger is called repeatedly.
    if logger.handlers:
        return logger

    logger.setLevel(level)

    # Create a simple, readable formatter.
    formatter: logging.Formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    # StreamHandler writes to stdout.
    handler: logging.Handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    logger.addHandler(handler)

    # Prevent logs from being duplicated by root logger handlers.
    logger.propagate = False

    return logger
