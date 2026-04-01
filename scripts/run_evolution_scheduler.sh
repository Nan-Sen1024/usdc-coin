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

echo "Using Python interpreter: ${PYTHON_CMD[*]}"
echo "[1/2] Run one evolution cycle"
"${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
  --config "$CONFIG_PATH" \
  --evolution-spec "$EVOLUTION_SPEC" \
  --evolution-state-dir "$EVOLUTION_STATE_DIR" \
  --evolution-cycle

if [[ "$DISPATCH_MODE" == "off" ]]; then
  echo "[2/2] Dispatch skipped because DISPATCH_MODE=off"
  exit 0
fi

echo "[2/3] Propose or reuse the next candidate when needed"
"${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
  --config "$CONFIG_PATH" \
  --evolution-spec "$EVOLUTION_SPEC" \
  --evolution-state-dir "$EVOLUTION_STATE_DIR" \
  --evolution-propose-candidate || true

echo "[3/3] Seed candidate bundle if an active candidate exists"
"${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
  --config "$CONFIG_PATH" \
  --evolution-spec "$EVOLUTION_SPEC" \
  --evolution-state-dir "$EVOLUTION_STATE_DIR" \
  --evolution-seed-candidate || true

echo "[4/4] Evaluate worker dispatch"
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
