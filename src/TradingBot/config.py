from __future__ import annotations

import os
from typing import Any
from dotenv import load_dotenv

load_dotenv(".env.secrets")

def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

paper: bool = _get_bool("ALPACA_PAPER", True)

api_key = os.getenv("ALPACA_PAPER_API_KEY" if paper else "ALPACA_LIVE_API_KEY", "")
api_secret = os.getenv("ALPACA_PAPER_API_SECRET" if paper else "ALPACA_LIVE_API_SECRET", "")

ALPACA_CONFIG: dict[str, Any] = {
    "api_key": api_key,
    "api_secret": api_secret,
    "paper": paper,
}


def validate_alpaca_config(cfg: dict) -> None:
    paper = cfg.get("paper")
    api_key = cfg.get("api_key", "")
    api_secret = cfg.get("api_secret", "")

    if paper is None:
        raise ValueError("ALPACA_CONFIG['paper'] is missing (expected True/False).")

    if not isinstance(api_key, str) or len(api_key.strip()) < 10:
        raise ValueError("Alpaca API key missing/invalid. Check .env.secrets and config mapping.")

    if not isinstance(api_secret, str) or len(api_secret.strip()) < 10:
        raise ValueError("Alpaca API secret missing/invalid. Check .env.secrets and config mapping.")
