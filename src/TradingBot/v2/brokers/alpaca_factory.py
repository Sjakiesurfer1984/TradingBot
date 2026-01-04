from __future__ import annotations

import os
from typing import Optional, Tuple

from TradingBot.v2.brokers.alpaca_broker_v2 import AlpacaBrokerV2
from TradingBot.v2.brokers.errors import MissingBrokerCredentialsError

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

logger = setup_logger("Alpaca Factory")


def _get_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _mask_secret(value: Optional[str], show_prefix: int = 4) -> str:
    """
    Mask secrets in logs.

    Why this exists
    - Logs must never leak API keys.
    - We still want enough information to confirm which key is in use.
    """
    if value is None:
        return "<missing>"
    v: str = value.strip()
    if not v:
        return "<missing>"
    if len(v) <= show_prefix:
        return "*" * len(v)
    return f"{v[:show_prefix]}{'*' * (len(v) - show_prefix)}"


def _alpaca_env_vars_for_mode(mode: str) -> Tuple[str, str]:
    mode_norm: str = mode.strip().lower()
    if mode_norm == "live":
        return ("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_API_SECRET")
    return ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET")


def build_alpaca_broker(request_timeout_seconds: float = 10.0) -> AlpacaBrokerV2:
    with log_scope("alpaca_factory.build_alpaca_broker", logger, extra=f"request_timeout_seconds={request_timeout_seconds}"):
        mode: str = (_get_env("ALPACA_MODE") or "paper").lower()
        logger.info("Alpaca mode resolved | mode=%s", mode)

        key_var, secret_var = _alpaca_env_vars_for_mode(mode)
        logger.info("Credential env vars selected | key_var=%s secret_var=%s", key_var, secret_var)

        api_key: Optional[str] = _get_env(key_var)
        api_secret: Optional[str] = _get_env(secret_var)

        logger.info("API key present | %s=%s", key_var, _mask_secret(api_key))
        logger.info("API secret present | %s=%s", secret_var, _mask_secret(api_secret))

        if api_key is None or api_secret is None:
            logger.error(
                "Missing Alpaca credentials | broker_name=%s required_env_vars=%s",
                f"alpaca({mode})",
                (key_var, secret_var),
            )
            raise MissingBrokerCredentialsError(
                broker_name=f"alpaca({mode})",
                required_env_vars=(key_var, secret_var),
            )

        paper: bool = mode != "live"
        logger.info("Alpaca paper flag resolved | paper=%s", paper)

        broker: AlpacaBrokerV2 = AlpacaBrokerV2(
            api_key=api_key,
            api_secret=api_secret,
            paper=paper,
            request_timeout_seconds=request_timeout_seconds,
        )

        logger.info("AlpacaBrokerV2 constructed | type=%s paper=%s", type(broker).__name__, paper)
        return broker
