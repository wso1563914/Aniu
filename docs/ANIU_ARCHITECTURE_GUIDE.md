# Aniu Architecture Guide

## 1. Purpose

This document is the baseline architecture guide for Aniu after the architecture direction has been agreed.

It is used to unify the follow-up refactoring, module splitting, data model evolution, frontend reorganization, and Agent capability expansion. If later detailed designs, implementation plans, or code changes conflict with this document, this document takes precedence unless a new architecture decision is explicitly made.

Current agreed architecture direction:

- DDD main architecture
- FSM Agent kernel
- Event-driven side effect layer

This is not a theoretical architecture exercise. It is the target architecture chosen based on the current codebase status of Aniu.

## 2. Product Definition

Aniu is a local-first stock trading intelligent agent system.

Its core goal is not to become a generic computer agent, nor to prioritize desktop packaging first. Its core goal is:

- continuously ingest market and account data
- analyze market conditions and positions with LLM support
- generate structured trade proposals under a controlled workflow
- let domain rules perform synchronous risk decisions
- execute legal simulated trades through controlled services
- expose the full run process, decision path, and execution results to the frontend
- accumulate review, memory, and strategy assets for future evolution

Aniu should ultimately become:

**an explainable, controllable, evolvable trading intelligent agent centered on stock trading workflows and data visibility**

## 3. Architectural Decision

Aniu's final architecture is defined as:

**DDD main architecture + FSM Agent kernel + event-driven side effect layer**

The project keeps four core engineering layers:

1. Infra Layer
2. Domain Layer
3. Agent Layer
4. Service & API Layer

In addition, it introduces one cross-layer mechanism:

- Event-driven side effect layer

Important clarification:

- The event-driven side effect layer is not the main architecture and does not replace the transaction control flow.
- The Agent Layer is intentionally separated as an engineering layer to make its boundary explicit, even though semantically it behaves like a specialized application runtime.
- The Domain Layer owns business rules and hard constraints, but does not directly call LLM or external APIs.

## 4. Why This Architecture Fits Aniu

The current codebase already shows clear structural pressure points:

- `backend/app/services/aniu_service.py` has become the central God Object and mixes login, settings, account aggregation, run execution, scheduling, chat, persistent automation session, run display shaping, and trading-related logic.
- `backend/app/api/router.py` is still a single large router file and does not reflect bounded contexts.
- `backend/app/services/scheduler_service.py` directly depends on `aniu_service.process_due_schedule()`.
- `backend/app/services/event_bus.py` currently serves mainly as in-memory SSE streaming, not as a durable run event system.
- `backend/app/db/models.py` already contains useful core entities, but critical run facts are still overly packed into JSON fields.
- The frontend already has `Overview`, `Tasks`, `Chat`, `Schedule`, and `Settings`, but the UI structure is still not aligned with a stable trading intelligent agent runtime model.

The selected architecture solves the real problems above:

- DDD stabilizes trading concepts, risk rules, run assets, and data semantics.
- FSM makes the Agent runtime explicit, controllable, and auditable.
- Event-driven side effects preserve streaming UX and extensibility without turning the whole system into a microkernel.

## 5. Goals And Non-Goals

### Goals

- clear domain boundaries around trading, account data, runs, review, memory, skills, settings, and schedules
- a controlled Agent runtime that can analyze dynamically but cannot bypass rules
- a synchronous and explicit risk gate before execution
- a persistent run timeline that can be streamed live and replayed later
- frontend pages that consume structured run and trading data instead of reverse engineering raw text
- a foundation for future review, memory, versioning, and optionally multi-agent collaboration

### Non-Goals

- event-driven microkernel as the system main architecture
- unrestricted free-form autonomous agent execution
- letting LLM directly execute trades without service and domain controls
- making Tauri or desktop packaging the primary architectural axis at this stage
- distributed system decomposition, message queue infrastructure, or plugin marketplace design at this stage

## 6. Layer Definitions

### 6.1 Infra Layer

Responsibility:

- integrate with external systems and low-level runtime capabilities
- expose pure infrastructure abilities without business decision making

Includes:

- MyQuant or MiaoXiang HTTP clients
- LLM clients
- SQLAlchemy persistence and database sessions
- migrations and schema evolution support
- vector store or full-text indexing implementation
- file storage and attachment storage
- scheduler trigger infrastructure
- skill loading and filesystem workspace access
- run event persistence adapters

Must not contain:

- trading risk rules
- proposal approval logic
- agent prompting decisions
- HTTP request orchestration

### 6.2 Domain Layer

Responsibility:

- define Aniu's trading language, models, rules, and constraints
- transform raw external payloads into stable internal models
- own hard-coded risk rules and core business invariants
- define review, memory extraction, and strategy evolution rules

Includes:

- account snapshot, portfolio snapshot, orders, trade proposal, execution intent, policy decision
- run state rules and run summary rules
- risk gate logic
- review rules
- memory candidate extraction rules
- skill candidate extraction rules
- schedule rules and next-run calculation
- domain event definitions

Must not contain:

- direct LLM calls
- direct HTTP calls to external providers
- direct FastAPI request or response handling
- SSE connection management

### 6.3 Agent Layer

Responsibility:

- act as the LLM-driven reasoning runtime
- move runs through an explicit FSM
- build prompts and coordinate controlled tool usage
- generate structured outputs for downstream domain and service handling

Includes:

- FSM runner
- state handlers
- prompt builder
- tool adapters
- optional future roundtable coordinator for multi-agent collaboration

Must not contain:

- direct database writes
- direct SQLAlchemy session access
- direct broker or trading client calls
- direct bypass of domain risk decisions

### 6.4 Service & API Layer

Responsibility:

- expose REST and SSE interfaces
- orchestrate use cases and transaction boundaries
- create runs and invoke the Agent runtime
- persist outputs and publish run events

This layer is kept as one architecture layer, but inside code it must remain split into two responsibilities:

- API responsibility: routes, request validation, response schemas, SSE connection handling
- service responsibility: use case orchestration, state advancement, event publishing, persistence coordination

Must not contain:

- raw infrastructure implementation details
- hard trading rules
- unmanaged agent autonomy

## 7. Event-Driven Side Effect Layer

Aniu explicitly uses events, but only as a side effect mechanism.

This mechanism is used for:

- live SSE streaming
- durable run timeline recording
- dashboard projection updates
- async review triggers
- memory indexing or vectorization
- notifications and observability

This mechanism is not used for:

- the main trading control path
- final risk approval
- bypassing transaction boundaries
- core execution authorization

The rule is simple:

**the main control flow stays synchronous and explicit; events only broadcast facts that already happened**

## 8. Core Bounded Contexts

Aniu should evolve around the following bounded contexts.

### 8.1 Trading Context

Owns:

- trade proposals
- execution intents
- policy decisions
- trade orders
- execution results

### 8.2 Market & Portfolio Context

Owns:

- account overview
- balance snapshots
- positions
- current orders
- trade summaries
- market snapshots
- trading calendar rules

### 8.3 Run Orchestration Context

Owns:

- strategy runs
- run state transitions
- run event timeline
- run outputs
- tool call trace summaries

### 8.4 Review & Evolution Context

Owns:

- daily reviews
- success and failure patterns
- strategy evaluation
- version comparison
- future replay and backfill rules

### 8.5 Memory & Skill Context

Owns:

- long-term memories
- skill metadata
- skill enable or disable state
- future memory retrieval and indexing

### 8.6 Settings & Schedule Context

Owns:

- app settings
- model settings
- trade switches
- schedule definitions
- automation parameters
- next-run rules

### 8.7 Interaction & Session Context

Owns:

- user chat sessions
- automation sessions
- message history
- attachments
- session summarization and compaction

This context remains important, but it must not absorb the trading core again.

## 9. Agent Runtime Definition

Aniu uses a fixed outer workflow and dynamic inner reasoning.

This means:

- the runtime protocol is fixed
- the analysis content and conclusions are dynamic

The default FSM is:

1. `Observe`
2. `Analyze`
3. `Propose`
4. `PolicyCheck`
5. `Execute` or `Skip` or `Replan`
6. `Review`
7. `Completed`

State intent:

- `Observe`: collect account, positions, orders, market data, relevant memory, enabled skills, recent run context
- `Analyze`: form market interpretation, opportunity hypotheses, and risk concerns
- `Propose`: produce structured trade proposals or structured hold decisions
- `PolicyCheck`: invoke synchronous domain policy checks
- `Execute`: execute only approved simulated trade intents through services
- `Skip`: safely terminate when no action or no legal action should be taken
- `Replan`: retry proposal generation only under bounded and explicit policy revision conditions
- `Review`: generate run conclusion and prepare review assets
- `Completed`: terminal run state

Important rules:

- Agent can propose, but cannot self-authorize execution.
- Risk decisions are always synchronous and domain-owned.
- Replan is allowed only when domain returns a revision-friendly policy decision.
- Replan count must be bounded.

## 10. Policy Decision Model

The Domain Layer should not only return pass or fail.

Aniu's policy decision should support at least three outcomes:

- `approved`
- `revise`
- `rejected`

Meaning:

- `approved`: legal to execute
- `revise`: proposal can be adjusted and resubmitted under constraints
- `rejected`: no further proposal retry should continue for this run branch

The Domain Layer only returns structured policy decisions.

The Agent runtime or coordinating service decides the next step:

- `approved` -> `Execute`
- `revise` -> `Replan`
- `rejected` -> `Skip`

The Domain Layer must never directly call the Agent back.

## 11. Scheduled Autonomous Trade Run Flow

When a schedule triggers a market-analysis-and-autonomous-trade task, the system should run like this:

1. Scheduler detects a due enabled schedule.
2. Scheduler triggers a service command such as `StartScheduledRun`.
3. Service layer creates a `StrategyRun` record.
4. Service layer publishes `RunStarted`.
5. Agent FSM enters `Observe` and collects market, account, memory, and skill context through controlled tools.
6. Agent enters `Analyze` and produces structured analysis.
7. Agent enters `Propose` and produces structured trade proposals or a structured hold result.
8. Service layer invokes domain `PolicyCheck` synchronously.
9. If approved, service layer creates execution intents and calls trading execution services.
10. Infra layer submits simulated orders through the trading client.
11. Results are persisted into `trade_orders`, `strategy_runs`, and `run_events`.
12. Agent enters `Review` and generates the run conclusion.
13. Service layer marks the run as completed or failed.
14. Event subscribers update live stream consumers, projections, review triggers, and memory indexing.

The critical invariant is:

**proposal -> policy check -> execute**

This control path remains synchronous, explicit, and auditable.

## 12. Core Event Model

Aniu should standardize around durable run events.

Recommended event types:

- `RunStarted`
- `StateEntered`
- `ObservationCaptured`
- `AnalysisGenerated`
- `ProposalGenerated`
- `PolicyApproved`
- `PolicyRejected`
- `PolicyReviseRequested`
- `OrderRequested`
- `OrderSubmitted`
- `OrderFailed`
- `ReviewDraftGenerated`
- `RunCompleted`
- `RunFailed`

Recommended common fields:

- `event_id`
- `run_id`
- `sequence`
- `event_type`
- `state_name`
- `created_at`
- `payload`

Future-compatible optional fields for multi-agent extension:

- `actor_id`
- `actor_role`
- `round_index`
- `proposal_version`

## 13. Data Model Direction

Current important tables already present:

- `app_settings`
- `strategy_schedules`
- `strategy_runs`
- `trade_orders`
- `chat_sessions`
- `chat_messages`
- `chat_attachments`

Target model direction adds durable run and evolution assets.

Required additions or structural upgrades:

- `run_events`
- `daily_reviews`
- `memories`
- `skills`
- `strategy_versions`

Design direction for key tables:

- `strategy_runs`: run header, run status, final output, major summary fields
- `run_events`: append-only fact log for timeline and replay
- `trade_orders`: execution records tied to run and proposal lineage
- `daily_reviews`: review products and evolution summaries
- `memories`: accepted long-term experience assets
- `skills`: persisted skill metadata and future evolution linkage
- `strategy_versions`: version lineage for prompts, policies, and strategy configurations

The project should gradually reduce reliance on large opaque JSON blobs as the only source of truth for important run facts.

## 14. Frontend Architecture Direction

The frontend should evolve from a tabbed web UI into a trading intelligent agent workbench.

Core frontend views should be organized around:

- `Overview`
- `Runs`
- `Run Detail Timeline`
- `Chat`
- `Schedule`
- `Settings`
- `Review`
- `Memory / Skills`

Data rules:

- normal query and page loading use REST
- real-time run progress uses SSE
- SSE appends timeline facts and local live state, but does not perform business decisions in the browser
- the frontend should consume structured outputs, not reverse engineer free-form final text

Current view mapping:

- `frontend/src/views/OverviewView.vue` remains the account and trading overview surface
- `frontend/src/views/TasksView.vue` evolves into `Runs + Run Detail Timeline`
- `frontend/src/views/ChatView.vue` remains an interaction view, but not the transaction control center
- `frontend/src/views/ScheduleView.vue` remains the schedule management surface
- `frontend/src/views/SettingsView.vue` remains the system configuration surface

## 15. Target Backend Structure

The long-term target structure is:

```text
backend/
  app/
    api/
      routes/
        account.py
        runs.py
        chat.py
        schedules.py
        settings.py
        skills.py
        reviews.py
      sse/
        run_stream.py
        chat_stream.py

    services/
      settings_service.py
      schedule_service.py
      run_service.py
      run_query_service.py
      account_service.py
      trading_service.py
      review_service.py
      automation_session_service.py
      skill_admin_service.py

    agent/
      kernel/
        runner.py
        fsm.py
        prompt_builder.py
      states/
        observe.py
        analyze.py
        propose.py
        policy_check.py
        execute.py
        review.py
      tools/
        market_tools.py
        trading_tools.py
        memory_tools.py
        review_tools.py

    domain/
      market/
      trading/
      run/
      review/
      memory/
      skills/
      schedule/
      settings/
      shared/

    events/
      bus.py
      publisher.py
      subscribers/
        sse_publisher.py
        run_logger.py
        projection_updater.py
        review_trigger.py
        memory_indexer.py

    infra/
      db/
      repositories/
      clients/
      scheduler/
      vector/
      storage/

    schemas/
      api/
      dto/
```

## 16. Migration Mapping From Current Code

### 16.1 `backend/app/services/aniu_service.py`

This file must be gradually decomposed.

Target destinations include:

- settings-related behavior -> `settings_service` and `domain/settings`
- schedule behavior -> `schedule_service` and `domain/schedule`
- run lifecycle behavior -> `run_service`, `run_query_service`, and `domain/run`
- account aggregation and shaping -> `account_service` and `domain/market`
- trading proposal and execution coordination -> `trading_service` and `domain/trading`
- persistent automation session behavior -> `automation_session_service`
- agent orchestration logic -> `agent/`
- display-oriented shaping logic -> query services and DTO builders

The project should not preserve a single future God Object in another name.

### 16.2 `backend/app/api/router.py`

This file should be split by bounded context.

Recommended route groups:

- settings routes
- skill routes
- schedule routes
- run routes
- account routes
- chat routes
- review routes

### 16.3 `backend/app/services/event_bus.py`

This should evolve into a durable run event subsystem.

The current per-run in-memory fan-out model can be retained as the live streaming core, but it must be complemented by persistent event logging.

### 16.4 `backend/app/services/scheduler_service.py`

This should become a scheduler trigger layer that starts commands or services, instead of directly invoking the God Object.

### 16.5 `backend/app/services/chat_session_service.py`

This should remain a distinct interaction and session service, while staying isolated from trading core orchestration.

### 16.6 `backend/app/skills/*` and `skill_admin_service.py`

These stay valuable and should be preserved as part of the Memory & Skill context, with clearer boundaries between:

- skill loading
- skill metadata
- skill runtime execution
- skill management APIs

## 17. Architecture Guardrails

The following rules are mandatory for future changes.

1. Agent code must not directly access database sessions.
2. Agent code must not directly call external trading clients.
3. Agent code must not bypass domain policy checks.
4. Domain code must not call LLMs directly.
5. API routes must not directly call raw infrastructure clients.
6. Core risk checks must be synchronous and domain-owned.
7. Event subscribers must not secretly advance the main transaction control path.
8. Important state changes must be persisted before broadcasting terminal facts.
9. Every simulated order must remain traceable to `run_id + proposal + policy decision + execution result`.
10. Review, memory, and skill generation must not block main run completion.
11. New frontend behavior should align with stable structured backend contracts, not inferred text parsing.
12. Single-process local deployment remains the default until there is a proven need for more infrastructure.

## 18. Future Extension Policy

### 18.1 Multi-Agent Roundtable

This architecture supports future multi-agent roundtable collaboration, but only inside the Agent Layer.

Rules:

- multi-agent coordination must stay inside `Analyze` and `Propose` style phases
- only one synthesized proposal set leaves the Agent Layer
- domain policy and service execution remain unchanged
- no Agent role may directly execute trades

### 18.2 Desktop Packaging

Desktop packaging or Tauri integration may be added later as a host-layer concern, but it is not the current core architecture axis.

### 18.3 Event System Growth

The event layer may grow in capability, but Aniu should still avoid turning the entire architecture into an event-driven microkernel unless product scope changes materially.

## 19. Refactoring Baseline Sequence

Future detailed refactoring plans should follow this high-level order:

1. split the God Object and stabilize service boundaries
2. introduce durable `run_events` and standard run event types
3. establish the FSM Agent kernel and explicit run states
4. move hard rules into domain risk and domain models
5. align frontend modules and run detail UI with structured events and outputs
6. add review, memory, and versioning assets
7. consider multi-agent collaboration only after the single-agent controlled workflow is stable

## 20. Final Statement

Aniu is not being optimized into a prettier shell around a large service file.

Aniu is being reshaped into:

**a trading-domain-centered, FSM-controlled, event-observable, and evolution-ready local intelligent agent system**

All future architecture refinements, implementation tasks, and restructuring plans should use this document as the baseline guide.
