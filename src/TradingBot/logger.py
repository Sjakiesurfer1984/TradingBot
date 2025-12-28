# TradingBot/logger.py

import logging


def setup_logger(name: str) -> logging.Logger:
    """
    Return a logger instance that prints multiline output with delimiters
    around each log message, including file name and line number.

    Args:
        name (str): Name for the logger.

    Returns:
        logging.Logger: Configured logger with a StreamHandler.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "---------- %(asctime)s - %(levelname)s - %(filename)s:%(lineno)d ----------\n"
            "%(message)s\n"
            "----------------------------------------\n"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
