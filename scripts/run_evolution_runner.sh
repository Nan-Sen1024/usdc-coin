#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CONFIG_PATH:-$ROOT_DIR/config/config.yaml}"
EVOLUTION_SPEC="${EVOLUTION_SPEC:-$ROOT_DIR/strategy-evolution.yaml}"
EVOLUTION_STATE_DIR="${EVOLUTION_STATE_DIR:-$ROOT_DIR/data/evolution}"
MODE="${MODE:-seed}"
REPORT_PATH="${REPORT_PATH:-}"

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

case "$MODE" in
  seed)
    "${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
      --config "$CONFIG_PATH" \
      --evolution-spec "$EVOLUTION_SPEC" \
      --evolution-state-dir "$EVOLUTION_STATE_DIR" \
      --evolution-seed-candidate
    ;;
  apply-report)
    if [[ -z "$REPORT_PATH" ]]; then
      echo "REPORT_PATH is required when MODE=apply-report" >&2
      exit 1
    fi
    "${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
      --config "$CONFIG_PATH" \
      --evolution-spec "$EVOLUTION_SPEC" \
      --evolution-state-dir "$EVOLUTION_STATE_DIR" \
      --evolution-apply-evaluation-report "$REPORT_PATH"
    ;;
  *)
    echo "Unsupported MODE=$MODE. Use MODE=seed or MODE=apply-report." >&2
    exit 1
    ;;
esac
