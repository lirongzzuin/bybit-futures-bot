#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Backtest Runner Helper
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$SCRIPT_DIR")"
VENV="$BOT_DIR/venv"

PYTHON="${VENV}/bin/python"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi

SYMBOLS="${SYMBOLS:-BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT}"
YEARS="${YEARS:-5}"
INTERVAL="${INTERVAL:-15}"

echo "====================================="
echo "  Bybit Bot Backtest Runner"
echo "  Symbols: $SYMBOLS"
echo "  Years:   $YEARS"
echo "  Interval: ${INTERVAL}m"
echo "====================================="

cd "$BOT_DIR"

# Load .env if it exists
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

mkdir -p logs reports data_cache

"$PYTHON" -m src.backtest.engine \
    --symbols $SYMBOLS \
    --years "$YEARS" \
    --interval "$INTERVAL"

echo ""
echo "Report saved to: reports/"
ls -lh reports/*.html 2>/dev/null | tail -5 || true
