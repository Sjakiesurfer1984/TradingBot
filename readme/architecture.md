
---

## `readme/architecture.md`

```markdown
# Architecture and responsibilities

This project has four layers. Keeping these boundaries clean makes unit testing easy.

## Layer 1: Infrastructure (external systems)
Goal: Hide third party APIs behind a stable interface.

Modules:
- brokers/alpaca_broker.py
- brokers/broker_interface.py (recommended)

Responsibilities:
- Translate your internal method calls into broker API calls
- Return normalised Python types (floats, dicts, DataFrames)
- Never contain strategy logic

Core interface methods used today:
- get_asset_price(symbol: str) -> float
- get_positions() -> list[dict]
- get_account_info() -> dict
- get_option_chain(symbol: str, tipo: str, strike: tuple[float, float], expiration: tuple[date, date]) -> pandas.DataFrame
- get_option_buying_power() -> float (recommended)

## Layer 2: Domain (trading logic)
Goal: Strategy is pure business logic plus broker calls through the interface.

Modules:
- strategies/strategy_interface.py
- strategies/poormans_covered_call.py

Responsibilities:
- Decide what to trade and when
- Compute strike and expiration ranges
- Select contracts from option chain
- Enforce strategy rules and guardrails
- Ask broker to place orders (through broker interface)

Data types in and out:
- BotState (mutable state object)
- pandas.DataFrame for option chains
- pandas.Series for a selected contract row

## Layer 3: Application (orchestration)
Goal: Wire up objects and run the loop.

Modules:
- main.py
- scheduler.py

Responsibilities:
- Construct instances
- Provide configuration to strategies
- Run strategy on a schedule
- Catch and log errors

## Layer 4: Support (utilities and observability)
Goal: Helpers and diagnostics with no side effects beyond logging.

Modules:
- utilities/option_utils.py
- logger.py
- risk_manager.py (to be wired)
- bot_state.py

Responsibilities:
- Option selection helper functions
- Logging configuration
- Track bot state (cash, positions, active orders)
- Risk checks (position sizing, max drawdown, etc)

## Immediate robustness notes (affects testing)
- In the strategy, order submission currently reaches into `broker.trading_client.submit_order(...)`.
  This breaks the broker abstraction. The Strategy should call a broker method like `submit_order(order_request)` instead.
- `_calculate_order_quantity` currently calls `self.broker.get_option_buying_power()()` which looks like an accidental double call.
  This will cause runtime errors and makes unit testing harder.
- `config.py` currently contains secrets. Move these into environment variables early, even in paper trading.
