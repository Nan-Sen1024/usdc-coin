# Progressive Drought Release Concession Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make drought-mode rebalance release orders concede more price only after inventory has been stuck for longer, so live inventory can actually unwind instead of sitting above market for days.

**Architecture:** Reuse the existing drought guard and `inventory_repair_steps` path inside `MicroMakerStrategy`. Keep the change single-factor: only adjust drought extra negative ticks over time, and only for release-mode rebalance orders. Do not change release sizing, entry suppression, or drought trigger thresholds in this plan.

**Tech Stack:** Python, pytest, existing strategy/state models in `src/`

---

### Task 1: Lock the Current Drought Behavior With Tests

**Files:**
- Modify: `tests/test_strategy.py`
- Read: `src/strategy.py`

**Step 1: Write the failing tests**

Add tests that cover:
- drought sell release still adds exactly `1` extra negative tick at the first aged step
- older drought sell release can concede more than `1` tick, capped at the intended ceiling
- non-drought release behavior remains unchanged
- buy-side drought behavior mirrors the same progressive concession logic

**Step 2: Run tests to verify at least one fails**

Run: `pytest tests/test_strategy.py -q -k 'drought_release'`
Expected: FAIL because the current code only returns a fixed extra tick.

**Step 3: Commit**

Do not commit yet. Keep the failing tests local until implementation passes.

### Task 2: Implement Progressive Drought Concession

**Files:**
- Modify: `src/strategy.py`
- Test: `tests/test_strategy.py`

**Step 1: Write the minimal implementation**

Change only the drought concession helper(s) so that:
- the first drought concession remains the current behavior
- extra negative ticks can increase with `inventory_repair_steps` / lot age
- the total drought concession is capped conservatively at `3` ticks unless the existing code already provides a smaller natural bound
- the logic is isolated to drought-triggered rebalance release paths

**Step 2: Preserve non-goals**

Do not change:
- `sell_drought_rebalance_window_seconds`
- `sell_drought_inventory_ratio_pct`
- `rebalance_release_excess_only`
- release size/depth geometry
- entry-side logic

**Step 3: Run targeted tests**

Run: `pytest tests/test_strategy.py -q -k 'drought_release'`
Expected: PASS

### Task 3: Run Touched-Path Regression

**Files:**
- Read only

**Step 1: Run strategy regression**

Run: `pytest tests/test_strategy.py -q`
Expected: PASS

**Step 2: Run syntax verification**

Run: `python3 -m py_compile src/strategy.py tests/test_strategy.py`
Expected: PASS

### Task 4: Update Evolution Record

**Files:**
- Modify: `strategy-evolution.yaml`

**Step 1: Record the challenger**

Add or update a candidate entry describing:
- the exact hypothesis
- that this is a price-only drought release experiment
- why it was chosen over the window-only challenger
- what live metrics should be checked after deployment

**Step 2: Keep scope tight**

Do not rewrite unrelated diagnosis history. Only append or minimally update the candidate/decision state needed for this experiment.

### Task 5: Final Verification Summary

**Files:**
- Read only

**Step 1: Summarize evidence**

Include:
- which tests were added
- which commands passed
- exact files changed
- any residual risk, especially the tradeoff between faster unwind and lower realized edge per trade

**Step 2: Commit**

```bash
git add docs/plans/2026-04-04-progressive-drought-release-concession.md src/strategy.py tests/test_strategy.py strategy-evolution.yaml
git commit -m "feat: scale drought release concession with inventory age"
```

Only commit if explicitly requested.
