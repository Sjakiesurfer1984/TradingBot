from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class MissingBrokerCredentialsError(ValueError):
    """
    Raised when credentials for a broker are missing.

    Why this exists
    - Keeps main() broker-agnostic.
    - Provides a consistent, copy-paste friendly error message.
    """

    broker_name: str
    required_env_vars: Sequence[str]

    def __str__(self) -> str:
        required: str = ", ".join(self.required_env_vars)
        return (
            f"No credentials provided for {self.broker_name}. "
            f"Set these environment variables: {required}."
        )

class BrokerConnectionError(RuntimeError):
    def __init__(self, broker_name: str, message: str) -> None:
        super().__init__(f"{broker_name}: {message}")
        self.broker_name = broker_name
