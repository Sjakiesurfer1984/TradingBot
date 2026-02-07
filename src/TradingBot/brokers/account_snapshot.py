from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class AccountSnapshot:
    """
    Immutable account snapshot returned by the broker.

    Why this exists
    - Orchestrator needs multiple account values in a consistent point-in-time view.
    - Fetching /v2/account multiple times per cycle is wasteful and can drift.

    Notes
    - raw is kept for debugging because broker payloads can change.
    """
    equity: float
    options_buying_power: float
    cash: Optional[float]
    raw: Any
