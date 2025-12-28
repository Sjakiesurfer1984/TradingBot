# Runtime diagrams (how the program works)

## Block diagram (high level)

```mermaid
flowchart TB
  subgraph Application
    MAIN[main.py\nOrchestrator]
    SCHED[scheduler.py\nScheduler]
  end

  subgraph Domain
    STRAT[strategies/poormans_covered_call.py\nPoormansCoveredCall.execute(state)]
    ISTRAT[strategies/strategy_interface.py\nStrategy ABC]
  end

  subgraph Infrastructure
    BROKER[brokers/alpaca_broker.py\nAlpacaBroker]
  end

  subgraph Support
    STATE[bot_state.py\nBotState]
    UTILS[utilities/option_utils.py\nbuild_strike_range\nbuild_expiration_range\nselect_option_by_delta]
    LOG[logger.py\nsetup_logger]
    RISK[risk_manager.py\nRiskManager]
  end

  MAIN -->|constructs| BROKER
  MAIN -->|constructs| STATE
  MAIN -->|constructs| SCHED
  MAIN -->|constructs| STRAT

  SCHED -->|calls every N minutes| BT[bot_task()]
  BT -->|calls| STRAT
  STRAT -->|reads/updates| STATE
  STRAT -->|broker calls| BROKER
  STRAT -->|uses| UTILS

  MAIN --> LOG
  STRAT --> LOG
  STATE --> LOG
  SCHED --> LOG


sequenceDiagram
  participant Main as main.py
  participant Scheduler as Scheduler.run
  participant Task as bot_task
  participant Strategy as PoormansCoveredCall.execute
  participant State as BotState
  participant Broker as AlpacaBroker
  participant Utils as option_utils

  Main->>Broker: AlpacaBroker(config)  (instance)
  Main->>State: BotState()  (instance)
  Main->>Strategy: PoormansCoveredCall(broker, symbol, config)  (instance)
  Main->>Scheduler: Scheduler(interval_minutes=5)
  Main->>Scheduler: run(bot_task)

  loop every interval
    Scheduler->>Task: call bot_task() -> None
    Task->>Strategy: execute(state: BotState) -> None

    Strategy->>State: refresh_account_info(broker) -> None
    State->>Broker: get_account_info() -> dict
    Broker-->>State: dict
    Strategy->>State: refresh_positions(broker) -> None
    State->>Broker: get_positions() -> list[dict]
    Broker-->>State: list[dict]

    Strategy->>Broker: get_asset_price(symbol: str) -> float
    Broker-->>Strategy: underlying_price: float

    Strategy->>Utils: build_strike_range(price: float, multipliers: tuple[float,float]) -> tuple[float,float]
    Utils-->>Strategy: strike_range: tuple[float,float]

    Strategy->>Utils: build_expiration_range(target_dte: int, padding: tuple[int,int]) -> tuple[date,date]
    Utils-->>Strategy: expiration_range: tuple[date,date]

    Strategy->>Broker: get_option_chain(symbol, tipo="call", strike_range, expiration_range) -> DataFrame
    Broker-->>Strategy: leap_chain: pandas.DataFrame

    Strategy->>Utils: select_option_by_delta(chain: DataFrame, target_dte: int, target_delta: float) -> Series
    Utils-->>Strategy: leap_option: pandas.Series

    Note over Strategy: Near-term call selection and order submission are currently commented out.
  end
