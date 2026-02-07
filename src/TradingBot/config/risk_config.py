from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    enable_open_order_dedupe: bool = True
