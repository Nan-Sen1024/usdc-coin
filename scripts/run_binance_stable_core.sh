#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OBS_CONFIG="${OBS_CONFIG:-$ROOT_DIR/config/config.binance.observe.yaml}"
USDC_CONFIG="${USDC_CONFIG:-$ROOT_DIR/config/config.binance.usdc.mainnet.yaml}"
USD1_USDT_CONFIG="${USD1_USDT_CONFIG:-$ROOT_DIR/config/config.binance.usd1usdt.mainnet.yaml}"
USD1_USDC_CONFIG="${USD1_USDC_CONFIG:-$ROOT_DIR/config/config.binance.usd1usdc.mainnet.yaml}"
OBS_QUOTE_SIZE="${OBS_QUOTE_SIZE:-5000}"
START_USD1_USDT="${START_USD1_USDT:-0}"
START_USD1_USDC="${START_USD1_USDC:-0}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"

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
echo "[1/4] Observe Binance stable markets before startup"
"${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
  --config "$OBS_CONFIG" \
  --observe-markets \
  --observe-inst-id USDC-USDT \
  --observe-inst-id USD1-USDT \
  --observe-inst-id USD1-USDC \
  --observe-quote-size "$OBS_QUOTE_SIZE"

echo "[2/4] Start USDC-USDT main engine"
nohup "${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
  --config "$USDC_CONFIG" \
  > "$LOG_DIR/binance_usdc_mainnet.out" 2>&1 &
USDC_PID=$!

USD1_USDT_PID=""
if [[ "$START_USD1_USDT" == "1" ]]; then
  echo "[3/4] Start USD1-USDT secondary engine"
  nohup "${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
    --config "$USD1_USDT_CONFIG" \
    > "$LOG_DIR/binance_usd1usdt_mainnet.out" 2>&1 &
  USD1_USDT_PID=$!
else
  echo "[3/4] Skip USD1-USDT secondary engine (set START_USD1_USDT=1 to enable it again)"
fi

USD1_USDC_PID=""
if [[ "$START_USD1_USDC" == "1" ]]; then
  echo "[4/4] Start USD1-USDC release leg"
  nohup "${PYTHON_CMD[@]}" "$ROOT_DIR/main.py" \
    --config "$USD1_USDC_CONFIG" \
    > "$LOG_DIR/binance_usd1usdc_mainnet.out" 2>&1 &
  USD1_USDC_PID=$!
else
  echo "[4/4] Skip USD1-USDC release leg (set START_USD1_USDC=1 to enable it again)"
fi

echo "Started Binance stable-core instances"
echo "  USDC-USDT pid: $USDC_PID log: $LOG_DIR/binance_usdc_mainnet.out"
if [[ -n "$USD1_USDT_PID" ]]; then
  echo "  USD1-USDT pid: $USD1_USDT_PID log: $LOG_DIR/binance_usd1usdt_mainnet.out"
else
  echo "  USD1-USDT pid: skipped"
fi
if [[ -n "${USD1_USDC_PID:-}" ]]; then
  echo "  USD1-USDC pid: $USD1_USDC_PID log: $LOG_DIR/binance_usd1usdc_mainnet.out"
else
  echo "  USD1-USDC pid: skipped"
fi
echo
echo "Use these commands to inspect:"
echo "  tail -f \"$LOG_DIR/binance_usdc_mainnet.out\""
echo "  tail -f \"$LOG_DIR/binance_usd1usdt_mainnet.out\""
echo "  tail -f \"$LOG_DIR/binance_usd1usdc_mainnet.out\""
