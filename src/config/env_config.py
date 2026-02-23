from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def _repo_root() -> Path:
    """
    Resolve repository root deterministically.

    This file is: <repo_root>/src/config/env_config.py
    So repo_root is parents[2].
    """
    return Path(__file__).resolve().parents[2]


def _load_root_dotenv() -> None:
    """
    Load <repo_root>/.env if present.

    override=False means real OS env vars win over .env.
    """
    dotenv_path = _repo_root() / ".env"
    load_dotenv(dotenv_path=dotenv_path, override=False)


@dataclass(frozen=True)
class AlpacaEnvConfig:
    mode: str  # "paper" | "live"


@dataclass(frozen=True)
class EnvConfig:
    broker_name: str
    alpaca: AlpacaEnvConfig


def _get_env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        return ""
    return v.strip()


def load_env_config() -> EnvConfig:
    _load_root_dotenv()

    return EnvConfig(
        broker_name=_get_env("BROKER_NAME", "alpaca").lower(), # default to alpaca for now since it's the only supported broker. To be set in .env 
        alpaca=AlpacaEnvConfig(mode=_get_env("ALPACA_MODE", "paper").lower()), # paper mode as default for safety
    )
