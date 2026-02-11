from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class CycleRunStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class CycleRunResult:
    started_at_utc: datetime
    ended_at_utc: datetime
    status: CycleRunStatus
    intents_count: int
    decisions_count: int
    submitted_count: int
    dry_run: bool
    error_message: Optional[str] = None


class OrchestratorABC(ABC):
    @abstractmethod
    def run_cycle(self) -> CycleRunResult:
        raise NotImplementedError
