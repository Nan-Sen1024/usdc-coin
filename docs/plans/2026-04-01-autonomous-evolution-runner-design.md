# Autonomous Evolution Runner Design

## Goal

Add a first-stage external optimization loop for `trend_bot_6` that can continue across short Codex sessions and real observation windows without relying on one long-lived chat or process.

## Scope

This design adds an external evolution layer around the existing bot and spec. It covers:

1. Persistent evolution state and evidence storage.
2. A deterministic cycle controller that reads the current spec and the latest audit artifacts.
3. CLI entrypoints so cron, systemd timers, or manual invocations can run one short optimization cycle at a time.

This design does not attempt to:

- rewrite strategy logic
- auto-edit exchange secrets
- auto-deploy code changes
- keep a single session alive indefinitely
- add a full backtest optimizer engine inside this repo

## Current Problem

The repository already has:

- a project spec in `strategy-evolution.yaml`
- bot runtime telemetry in snapshots, journals, and SQLite audit databases
- observer and summary commands

But it does not yet have a persistent controller that can answer:

- what the current champion is
- what candidate is under observation
- whether the latest observation passed promotion gates
- when the next optimization round should start
- how to continue after a process, machine, or chat session restarts

Without that controller, optimization remains ad hoc and tied to human memory or long chat context, which is fragile and prone to compaction and transport failures.

## Design

### 1. Evolution State Store

Add a dedicated evolution state directory under `data/evolution/`.

It will persist:

- `champion.yaml`: current promoted version and its evidence summary
- `controller_state.json`: current phase, next action, and last completed cycle
- `experiments.jsonl`: append-only experiment and candidate history
- `decisions.jsonl`: append-only promote/reject/rollback decisions
- `observations.jsonl`: append-only summaries of real runs and observation windows

This keeps long-lived memory on disk rather than in a chat session.

### 2. Deterministic Cycle Evaluator

Add a new evolution module that:

- loads `strategy-evolution.yaml`
- reads the latest audit snapshot and latest run metrics
- compares observation metrics against simple promotion and stop rules from the spec
- emits one deterministic decision:
  - `promote`
  - `reject`
  - `rollback`
  - `continue_observing`
  - `ready_for_optimization`
  - `interrupt`

The first-stage evaluator will intentionally stay conservative. It will use the metrics the repo already exposes instead of trying to infer deep alpha quality from incomplete data.

### 3. Observation-Driven Workflow

The cycle controller should support this loop:

1. Load spec and persistent state.
2. Summarize the latest observation from runtime artifacts.
3. Update observation history.
4. Decide whether:
   - the current candidate needs more observation
   - the candidate can be promoted
   - the candidate must be rejected or rolled back
   - a new optimization round should start
5. Persist the new controller state and decision log.

This is intentionally one-shot. A scheduler can invoke it every hour, every day, or after a run completes.

### 4. CLI Entry Points

Add new `main.py` flags:

- `--evolution-status`
- `--evolution-cycle`

`--evolution-status` should print a compact human-readable view of:

- spec target
- current phase
- latest observation
- champion status
- next action

`--evolution-cycle` should:

- read latest telemetry
- update the evolution store
- make one decision
- print the result

This keeps the external scheduler simple. It only needs to call one short command.

### 5. Minimal Promotion Logic

The first-stage promotion logic should require:

- cost realism remains enabled in the spec
- observed trade count meets the minimum
- no stop condition is triggered
- the latest observed primary metric is non-negative or improving relative to the previous baseline

If those conditions are not met, the controller should reject promotion and record the reason.

This is intentionally narrower than the full future design. The point of stage one is to create a stable control plane before adding richer optimization logic.

## File Layout

New files:

- `src/evolution_models.py`
- `src/evolution_store.py`
- `src/evolution_cycle.py`
- `tests/test_evolution_store.py`
- `tests/test_evolution_cycle.py`
- `scripts/run_evolution_cycle.sh`

Modified files:

- `main.py`

## Testing

Add focused coverage for:

- spec and controller state persistence
- append-only observation and decision logs
- cycle decision behavior for:
  - empty state
  - observation below minimum trade count
  - promotion candidate with improving metrics
  - stop-condition interruption

Run only focused tests for this change set.

## Risks

Main risk:

- The first-stage evaluator may make decisions from limited metrics and oversimplify promotion readiness.

Mitigation:

- Keep promotion conservative.
- Log explicit reasons for every decision.
- Treat this controller as a governance layer, not an optimizer oracle.

Secondary risk:

- The controller could accidentally overwrite user-maintained spec files.

Mitigation:

- Keep `strategy-evolution.yaml` read-mostly in this change.
- Persist controller and evidence state under `data/evolution/` instead of mutating strategy code or configs automatically.
