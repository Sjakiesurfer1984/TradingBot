# TradingBot V2 – Option C architecture

This document explains, end-to-end, how TradingBot V2 (Option C) works.

It covers:

* architectural philosophy
* control flow
* folder and file responsibilities
* data contracts between components
* how risk controls capital
* where strategies stop and risk begins

This is a design contract, not just documentation.

---

## 1. Core philosophy

TradingBot V2 follows a request -> evaluate -> execute model.

* Strategies request trades
* Risk decides if they are allowed and how large
* Orchestrator executes approved orders

### Hard rules

* Strategies never size positions
* Strategies never submit orders
* Strategies never query account state directly
* Risk is the single authority on capital allocation

If any of the above is violated, the programme has failed.

---

## 2. High-level system overview

```mermaid
flowchart LR
    Scheduler --> OrchestratorV2

    OrchestratorV2 -->|builds snapshot| RiskContext
    OrchestratorV2 -->|calls| StrategyV2
    StrategyV2 -->|produces| TradeIntent

    TradeIntent --> RiskEngineV2
    RiskContext --> RiskEngineV2

    RiskEngineV2 -->|approved| ApprovedOrder
    RiskEngineV2 -->|rejected| RejectedIntent

    ApprovedOrder -->|submit| Broker
```

---

## 3. Runtime control flow (one cycle)

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant O as OrchestratorV2
    participant C as RiskContext
    participant STR as StrategyV2
    participant R as RiskEngineV2
    participant B as Broker

    S->>O: run_cycle()
    O->>C: build snapshot
    O->>STR: generate_intents(ctx)
    STR-->>O: List[TradeIntent]
    O->>R: evaluate(ctx, intents)
    R-->>O: List[RiskDecision]
    O->>B: submit approved orders (unless dry-run)
```

---

## 4. Folder structure and responsibilities

```mermaid
flowchart TD
    TradingBot --> brokers
    TradingBot --> domain
    TradingBot --> strategies
    TradingBot --> utilities
    TradingBot --> v2
    TradingBot --> main.py
    TradingBot --> main_v2.py

    v2 --> context_py[context.py]
    v2 --> intents_py[intents.py]
    v2 --> decisions_py[decisions.py]
    v2 --> risk_engine_py[risk_engine.py]
    v2 --> orchestrator_py[orchestrator.py]
    v2 --> strategy_if[strategy_interface_v2.py]
    v2 --> rules

    rules --> open_order_rule_py[open_order_rule.py]
```

---

## 5. Core data contracts

### 5.1 RiskContext

```mermaid
classDiagram
    class RiskContext {
        datetime as_of_utc
        float option_buying_power
        float equity
        Dict positions
        List open_orders
        Dict prices
        get_price(symbol)
    }
```

Purpose:

* Immutable snapshot of account and market state
* Built once per orchestration cycle
* Shared read-only across strategies and risk

---

### 5.2 TradeIntent

```mermaid
classDiagram
    class TradeIntent {
        str intent_id
        str strategy_id
        str symbol
        payload
        str time_in_force
        Tuple tags
    }
```

TradeIntent represents desire, not permission.

---

### 5.3 Strategy-specific payload (PMCC example)

```mermaid
classDiagram
    class PmccIntentPayload {
        str underlying_symbol
        SelectedOption leap
        SelectedOption near
    }

    class SelectedOption {
        str option_symbol
        float ask_price
        float bid_price
        float delta
        int dte
    }

    TradeIntent --> PmccIntentPayload
    PmccIntentPayload --> SelectedOption
```

---

### 5.4 RiskDecision

```mermaid
classDiagram
    class RiskDecision {
        ApprovedOrder approved
        RejectedIntent rejected
    }

    class ApprovedOrder {
        str intent_id
        str client_order_id
        MultiLegLimitOrder order
    }

    class RejectedIntent {
        str intent_id
        str reason
    }

    RiskDecision --> ApprovedOrder
    RiskDecision --> RejectedIntent
```

---

## 6. Risk engine responsibilities

```mermaid
flowchart TB
    Intent --> DedupeRule
    DedupeRule -->|fail| Reject
    DedupeRule -->|pass| AllocationRule
    AllocationRule --> SizingRule
    SizingRule --> ApprovedOrder
```

---

## 7. Orchestrator responsibilities

The orchestrator:

* does not make trading decisions
* does not size trades
* does not contain strategy logic

Responsibilities:

1. Build RiskContext snapshot
2. Collect TradeIntents
3. Request RiskDecisions
4. Submit ApprovedOrders (or log in dry-run)

---

## 8. Strategy responsibilities (V2)

Strategies may:

* analyse markets
* select instruments
* express intent

Strategies may not:

* access capital
* submit orders
* decide risk

---

## 9. Comparison with V1

| Aspect           | V1         | V2 (Option C)     |
| ---------------- | ---------- | ----------------- |
| Position sizing  | Strategy   | Risk engine       |
| Capital limits   | Strategy   | Centralised       |
| Broker access    | Everywhere | Orchestrator only |
| Risk consistency | Fragmented | Guaranteed        |
| Auditability     | Low        | High              |

---

## 10. Mental model

```mermaid
flowchart LR
    Analyst[Strategy] --> Committee[Risk Engine]
    Committee --> Desk[Orchestrator]
    Desk --> Exchange[Broker]
```

---

## 11. Current implementation status

| Component          | Status   |
| ------------------ | -------- |
| V2 scaffolding     | complete |
| Data contracts     | complete |
| Deduplication rule | complete |
| Allocation policy  | pending  |
| Sizing logic       | pending  |
| Order submission   | pending  |
| Dry-run mode       | next     |

---

## 12. Key invariant

There must be no code path where a strategy can trade without passing through the risk engine.
