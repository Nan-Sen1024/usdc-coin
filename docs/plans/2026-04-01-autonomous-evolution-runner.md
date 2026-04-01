# Autonomous Evolution Runner Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a persistent, short-cycle evolution controller that can resume optimization decisions from on-disk state instead of relying on one long-lived session.

**Architecture:** Introduce new evolution modules for spec/state models, append-only evidence storage, and deterministic cycle evaluation. Expose the controller through new `main.py` flags so an external scheduler can run one cycle at a time. Keep strategy and execution logic untouched in this first stage.

**Tech Stack:** Python 3, dataclasses, YAML/JSON/JSONL persistence, existing audit SQLite telemetry, pytest

---

### Task 1: Add Evolution Models And Store

**Files:**
- Create: `src/evolution_models.py`
- Create: `src/evolution_store.py`
- Test: `tests/test_evolution_store.py`

**Step 1: Write the failing test**

Add tests that assert the evolution store can:

- create its directory structure
- persist controller state
- persist champion state
- append observations and decisions

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_evolution_store.py -q`

**Step 3: Write minimal implementation**

Create dataclasses and a store wrapper that reads and writes:

- `controller_state.json`
- `champion.yaml`
- `observations.jsonl`
- `decisions.jsonl`
- `experiments.jsonl`

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_evolution_store.py -q`

**Step 5: Commit**

Skip commit in this session unless explicitly requested.

### Task 2: Add Deterministic Cycle Evaluation

**Files:**
- Create: `src/evolution_cycle.py`
- Test: `tests/test_evolution_cycle.py`

**Step 1: Write the failing test**

Add tests for:

- initializing the controller with no previous state
- continuing observation when minimum trade count is not met
- promoting or marking ready when observed metrics improve
- interrupting when a stop condition is present

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_evolution_cycle.py -q`

**Step 3: Write minimal implementation**

Implement a cycle evaluator that:

- loads the project spec
- summarizes the latest observation
- updates state/logs
- returns a deterministic decision and next action

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_evolution_cycle.py -q`

**Step 5: Commit**

Skip commit in this session unless explicitly requested.

### Task 3: Expose CLI Entry Points

**Files:**
- Modify: `main.py`
- Create: `scripts/run_evolution_cycle.sh`
- Test: `tests/test_evolution_cycle.py`

**Step 1: Write the failing test**

Add coverage for a printable status or cycle output entrypoint if a focused unit hook is needed.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_evolution_cycle.py -q`

**Step 3: Write minimal implementation**

Add:

- `--evolution-status`
- `--evolution-cycle`
- optional `--evolution-spec`

Create a helper shell script that runs one cycle from repo root.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_evolution_cycle.py -q`

**Step 5: Commit**

Skip commit in this session unless explicitly requested.

### Task 4: Run Focused Verification

**Files:**
- Modify: `none`
- Test: `tests/test_evolution_store.py`
- Test: `tests/test_evolution_cycle.py`

**Step 1: Run the focused suite**

Run:

```bash
python3 -m pytest tests/test_evolution_store.py tests/test_evolution_cycle.py -q
```

**Step 2: Run syntax verification**

Run:

```bash
python3 -m py_compile main.py src/evolution_models.py src/evolution_store.py src/evolution_cycle.py
```

**Step 3: Summarize residual risk**

Record what remains unverified, especially the future optimizer-worker integration that this first stage intentionally leaves out.

**Step 4: Commit**

Skip commit in this session unless explicitly requested.
