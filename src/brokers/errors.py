from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class MissingBrokerCredentialsError(ValueError):
    broker_name:       str
    required_env_vars: Sequence[str]

    def __str__(self) -> str:
        return (
            f"No credentials for {self.broker_name}. "
            f"Set: {', '.join(self.required_env_vars)}"
        )


class BrokerConnectionError(RuntimeError):
    def __init__(self, broker_name: str, message: str) -> None:
        super().__init__(f"{broker_name}: {message}")
        self.broker_name = broker_name
