#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Deploy script — copies project to Oracle Cloud VM via SSH
# Usage: ./scripts/deploy.sh ubuntu@<your-oracle-ip>
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
    echo "Usage: $0 user@host"
    exit 1
fi

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_DIR="~/bybit-futures-bot"

echo "Deploying to $TARGET:$REMOTE_DIR ..."

# Sync code (exclude secrets and generated files)
rsync -avz --progress \
    --exclude=".env" \
    --exclude="*.db" \
    --exclude="logs/" \
    --exclude="reports/" \
    --exclude="data_cache/" \
    --exclude=".git/" \
    --exclude="venv/" \
    --exclude="__pycache__/" \
    --exclude="*.pyc" \
    --exclude=".pytest_cache/" \
    "$BOT_DIR/" \
    "$TARGET:$REMOTE_DIR/"

echo ""
echo "Code synced. Next steps on remote server:"
echo "  ssh $TARGET"
echo "  cd $REMOTE_DIR"
echo "  ./scripts/setup_oracle.sh   # first time only"
echo "  sudo systemctl restart bybit-bot"
echo ""
echo "Deployment done."
