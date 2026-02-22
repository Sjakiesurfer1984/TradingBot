from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List

from src.config.yaml_config import StrategySpec
from src.strategies.interfaces import StrategyABC
from src.utilities.logger import setup_logger

logger = setup_logger("StrategyRegistry")

StrategyBuilder = Callable[[StrategySpec], StrategyABC]


@dataclass
class StrategyBuilderRegistry:
    _builders: Dict[str, StrategyBuilder] = field(default_factory=dict, init=False)

    def register(self, name: str, builder: StrategyBuilder) -> None:
        self._builders[name.lower().strip()] = builder
        logger.info("Registered strategy builder | name=%s", name)

    def build_all(self, specs: List[StrategySpec]) -> List[StrategyABC]:
        strategies: List[StrategyABC] = []
        for spec in specs:
            key = str(spec.name).lower().strip()
            if key not in self._builders:
                raise KeyError(f"No strategy builder for '{spec.name}'. Registered: {list(self._builders)}")
            strategies.append(self._builders[key](spec))
        return strategies
