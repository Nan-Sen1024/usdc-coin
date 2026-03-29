#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${CONFIG_PATH:-$ROOT_DIR/config/config.binance.usd1usdt.collect.yaml}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
LOG_PATH="${LOG_PATH:-$LOG_DIR/binance_usd1usdt_collect.out}"

resolve_python_cmd() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    PYTHON_CMD=("${PYTHON_BIN}")
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    if python --version >/dev/null 2>&1; then
      PYTHON_CMD=("python")
      return 0
    fi
  fi

  if command -v py >/dev/null 2>&1; then
    if py -3 --version >/dev/null 2>&1; then
      PYTHON_CMD=("py" "-3")
      return 0
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    if python3 --version >/dev/null 2>&1; then
      PYTHON_CMD=("python3")
      return 0
    fi
  fi

  if command -v python.exe >/dev/null 2>&1; then
    PYTHON_CMD=("python.exe")
    return 0
  fi

  if command -v py.exe >/dev/null 2>&1; then
    PYTHON_CMD=("py.exe" "-3")
    return 0
  fi

  return 1
}

if ! resolve_python_cmd; then
  echo "No usable Python interpreter found." >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

echo "Using Python interpreter: ${PYTHON_CMD[*]}"
echo "Using config: $CONFIG_PATH"
echo "Log path: $LOG_PATH"

nohup "${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" --config "$CONFIG_PATH" > "$LOG_PATH" 2>&1 &
PID=$!

echo "Started Binance USD1->USDT collection bot"
echo "  pid: $PID"
echo "  log: $LOG_PATH"
