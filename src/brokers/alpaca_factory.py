from __future__ import annotations

import os
from typing import Optional

from src.brokers.alpaca_broker import AlpacaBroker
from src.brokers.errors import MissingBrokerCredentialsError
from src.utilities.logger import setup_logger

logger = setup_logger("AlpacaFactory")


def _env(name: str) -> Optional[str]:
    v = os.getenv(name, "").strip()
    return v or None


def _mask(v: Optional[str], n: int = 4) -> str:
    if not v:
        return "<missing>"
    return v[:n] + "*" * max(0, len(v) - n)


def build_alpaca_broker(request_timeout_seconds: float = 10.0) -> AlpacaBroker:
    mode = (_env("ALPACA_MODE") or "paper").lower()
    creds = {
        "paper": ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET"),
        "live":  ("ALPACA_LIVE_API_KEY",  "ALPACA_LIVE_API_SECRET"),
    }
    key_var, sec_var = creds.get(mode, creds["paper"])

    api_key    = _env(key_var)
    api_secret = _env(sec_var)

    logger.info("Alpaca mode=%s key=%s", mode, _mask(api_key))

    if api_key is None or api_secret is None:
        raise MissingBrokerCredentialsError(
            broker_name=f"alpaca({mode})",
            required_env_vars=(key_var, sec_var),
        )

    return AlpacaBroker(
        api_key=api_key,
        api_secret=api_secret,
        paper=(mode != "live"),
        request_timeout_seconds=request_timeout_seconds,
    )
