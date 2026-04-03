#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CONFIG_PATH:-$ROOT_DIR/config/config.yaml}"
EVOLUTION_SPEC="${EVOLUTION_SPEC:-$ROOT_DIR/strategy-evolution.yaml}"
EVOLUTION_STATE_DIR="${EVOLUTION_STATE_DIR:-$ROOT_DIR/data/evolution}"
CODEX_BIN="${CODEX_BIN:-codex}"
DISPATCH_MODE="${DISPATCH_MODE:-execute}"

resolve_python_cmd() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    PYTHON_CMD=("${PYTHON_BIN}")
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=("python3")
    return 0
  fi

  if command -v py >/dev/null 2>&1; then
    PYTHON_CMD=("py" "-3")
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    PYTHON_CMD=("python")
    return 0
  fi

  return 1
}

if ! resolve_python_cmd; then
  echo "No usable Python interpreter found. Set PYTHON_BIN explicitly." >&2
  exit 1
fi

mkdir -p "$EVOLUTION_STATE_DIR"

STEP_TOTAL=4
if [[ "$DISPATCH_MODE" == "off" ]]; then
  STEP_TOTAL=2
fi

echo "Using Python interpreter: ${PYTHON_CMD[*]}"
echo "[1/$STEP_TOTAL] Run one evolution cycle"
"${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
  --config "$CONFIG_PATH" \
  --evolution-spec "$EVOLUTION_SPEC" \
  --evolution-state-dir "$EVOLUTION_STATE_DIR" \
  --evolution-cycle

if [[ "$DISPATCH_MODE" == "off" ]]; then
  echo "[2/$STEP_TOTAL] Dispatch skipped because DISPATCH_MODE=off"
  exit 0
fi

readarray -t PREPARE_STATE < <("${PYTHON_CMD[@]}" - "$EVOLUTION_STATE_DIR" <<'PY'
import json
import sys
from pathlib import Path

state_path = Path(sys.argv[1]) / "controller_state.json"
payload = {}
if state_path.exists():
    payload = json.loads(state_path.read_text(encoding="utf-8"))
phase = str(payload.get("phase") or "")
next_action = str(payload.get("next_action") or "")
should_prepare = phase != "interrupted" and next_action in {
    "generate_next_candidate",
    "narrow_search",
    "prepare_next_challenger",
}
print("yes" if should_prepare else "no")
print(f"phase={phase or 'none'}, next_action={next_action or 'none'}")
PY
)

if [[ "${PREPARE_STATE[0]:-no}" == "yes" ]]; then
  echo "[2/$STEP_TOTAL] Propose or reuse the next candidate when needed"
  "${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
    --config "$CONFIG_PATH" \
    --evolution-spec "$EVOLUTION_SPEC" \
    --evolution-state-dir "$EVOLUTION_STATE_DIR" \
    --evolution-propose-candidate || true

  echo "[3/$STEP_TOTAL] Seed candidate bundle if an active candidate exists"
  "${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
    --config "$CONFIG_PATH" \
    --evolution-spec "$EVOLUTION_SPEC" \
    --evolution-state-dir "$EVOLUTION_STATE_DIR" \
    --evolution-seed-candidate || true
else
  echo "[2/$STEP_TOTAL] Candidate proposal skipped: ${PREPARE_STATE[1]:-phase=none, next_action=none}"
  echo "[3/$STEP_TOTAL] Candidate bundle seeding skipped: ${PREPARE_STATE[1]:-phase=none, next_action=none}"
fi

echo "[4/$STEP_TOTAL] Evaluate worker dispatch"
DISPATCH_FLAG="--evolution-dispatch"
if [[ "$DISPATCH_MODE" == "plan" ]]; then
  DISPATCH_FLAG="--evolution-dispatch-plan"
fi

"${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
  --config "$CONFIG_PATH" \
  --evolution-spec "$EVOLUTION_SPEC" \
  --evolution-state-dir "$EVOLUTION_STATE_DIR" \
  --evolution-codex-bin "$CODEX_BIN" \
  "$DISPATCH_FLAG"
