# TradingBot (PMCC) - project overview

This project runs a Poorman's Covered Call strategy on a schedule.

The system is intentionally modular so that:
- broker interaction can be swapped (Alpaca now, IBKR later)
- strategies are plug-in classes
- state tracking is isolated
- each module can be tested independently

## Current folder structure (as in the repo)

```text
TradingBot/
  brokers/
    alpaca_broker.py           # broker adaptor (not shown in this summary)
    broker_interface.py        # broker contract (recommended)
  strategies/
    poormans_covered_call.py   # strategy implementation
    strategy_interface.py      # Strategy ABC
  utilities/
    option_utils.py            # option selection helpers
  tests/
    test_*.py                  # unit tests (to be expanded)
  logger.py                    # logging setup
  bot_state.py                 # bot state dataclass
  scheduler.py                 # fixed-interval loop
  risk_manager.py              # risk checks (not wired yet)
  config.py                    # config values (move secrets to env)
  main.py                      # entry point and orchestration
  requirements.txt
