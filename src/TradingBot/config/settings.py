from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _get_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    s = value.strip()
    return s if s else None


def _get_env_bool(name: str, default: bool) -> bool:
    raw = _get_env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y"}


@dataclass(frozen=True)
class AlpacaConfig:
    mode: str  # "paper" or "live"
    api_key: str
    api_secret: str


@dataclass(frozen=True)
class AppConfig:
    broker_name: str
    dry_run: bool
    alpaca: AlpacaConfig


def load_app_config_from_env() -> AppConfig:
    """
    Loads config from environment variables (which come from .env via load_dotenv in main).

    Contract:
    - .env holds secrets and toggles
    - code reads env and constructs a typed config object
    """
    broker_name = _get_env("TRADINGBOT_BROKER").strip()
    print(broker_name)

    dry_run = _get_env_bool("TBOT_DRY_RUN", default=True)

    alpaca_mode = _get_env("ALPACA_MODE") or "paper"
    alpaca_mode = alpaca_mode.lower().strip()

    if alpaca_mode not in {"paper", "live"}:
        raise ValueError(f"ALPACA_MODE must be 'paper' or 'live', got '{alpaca_mode}'")

    if alpaca_mode == "paper":
        key = _get_env("ALPACA_PAPER_API_KEY") or ""
        secret = _get_env("ALPACA_PAPER_API_SECRET") or ""
    else:
        key = _get_env("ALPACA_LIVE_API_KEY") or ""
        secret = _get_env("ALPACA_LIVE_API_SECRET") or ""

    alpaca = AlpacaConfig(mode=alpaca_mode, api_key=key, api_secret=secret)

    return AppConfig(
        broker_name=broker_name,
        dry_run=dry_run,
        alpaca=alpaca,
    )
