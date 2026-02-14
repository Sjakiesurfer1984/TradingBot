from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class AlpacaEnvConfig:
    mode: str
    api_key: str
    api_secret: str


@dataclass(frozen=True)
class EnvConfig:
    broker_name: str
    alpaca: AlpacaEnvConfig


def load_env_config() -> EnvConfig:
    """
    Loads environment variables (optionally from a .env file) and returns a typed EnvConfig.

    This function is the single source of truth for broker credentials.
    """
    load_dotenv()

    broker_name: str = os.getenv("TRADINGBOT_BROKER", "alpaca").strip().lower()
    mode: str = os.getenv("ALPACA_MODE", "paper").strip().lower()

    if mode not in {"paper", "live"}:
        raise ValueError(f"ALPACA_MODE must be 'paper' or 'live', got '{mode}'")

    if mode == "paper":
        api_key: str = os.getenv("ALPACA_PAPER_API_KEY", "").strip()
        api_secret: str = os.getenv("ALPACA_PAPER_API_SECRET", "").strip()
    else:
        api_key = os.getenv("ALPACA_LIVE_API_KEY", "").strip()
        api_secret = os.getenv("ALPACA_LIVE_API_SECRET", "").strip()

    return EnvConfig(
        broker_name=broker_name,
        alpaca=AlpacaEnvConfig(mode=mode, api_key=api_key, api_secret=api_secret),
    )
