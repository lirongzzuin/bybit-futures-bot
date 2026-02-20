#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Oracle Cloud Always Free — Bot Setup Script
# Run this on a fresh Ubuntu 22.04 VM (AMD, 1 GB RAM)
#
# Usage:
#   chmod +x scripts/setup_oracle.sh
#   ./scripts/setup_oracle.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOT_DIR="$HOME/bybit-futures-bot"
VENV="$BOT_DIR/venv"
SERVICE_NAME="bybit-bot"

echo "======================================================"
echo "  Bybit Futures Bot — Oracle Cloud Setup"
echo "======================================================"

# ── 1. System packages ────────────────────────────────────────
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip git curl wget tzdata logrotate

sudo timedatectl set-timezone Asia/Seoul

# ── 2. Clone / update repo ────────────────────────────────────
echo "[2/7] Setting up project directory..."
if [ -d "$BOT_DIR" ]; then
    echo "  Updating existing repo..."
    cd "$BOT_DIR" && git pull
else
    echo "  NOTE: Copy your project files to $BOT_DIR"
    echo "  You can use: scp -r . ubuntu@<ip>:$BOT_DIR"
    mkdir -p "$BOT_DIR"
fi

# ── 3. Python venv ────────────────────────────────────────────
echo "[3/7] Creating Python virtual environment..."
python3.11 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install -r "$BOT_DIR/requirements.txt"

# ── 4. Directories ────────────────────────────────────────────
echo "[4/7] Creating runtime directories..."
mkdir -p "$BOT_DIR"/{logs,reports,data_cache}

# ── 5. Environment file ───────────────────────────────────────
echo "[5/7] Setting up environment file..."
if [ ! -f "$BOT_DIR/.env" ]; then
    cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
    echo ""
    echo "  ⚠️  IMPORTANT: Edit $BOT_DIR/.env with your credentials before starting!"
    echo "  nano $BOT_DIR/.env"
    echo ""
fi

# ── 6. Systemd service ────────────────────────────────────────
echo "[6/7] Installing systemd service..."
sudo cp "$BOT_DIR/bot.service" "/etc/systemd/system/$SERVICE_NAME.service"

# Update paths in service file
sudo sed -i "s|/home/ubuntu|$HOME|g" "/etc/systemd/system/$SERVICE_NAME.service"
sudo sed -i "s|User=ubuntu|User=$(whoami)|g" "/etc/systemd/system/$SERVICE_NAME.service"
sudo sed -i "s|Group=ubuntu|Group=$(whoami)|g" "/etc/systemd/system/$SERVICE_NAME.service"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# ── 7. Log rotation ───────────────────────────────────────────
echo "[7/7] Configuring log rotation..."
sudo tee /etc/logrotate.d/bybit-bot > /dev/null <<EOF
$BOT_DIR/logs/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    copytruncate
}
EOF

echo ""
echo "======================================================"
echo "  Setup Complete!"
echo "======================================================"
echo ""
echo "Next steps:"
echo "  1. Edit environment: nano $BOT_DIR/.env"
echo "  2. Run backtest:    $VENV/bin/python -m src.backtest.engine"
echo "  3. If backtest passes, set QUALITY_GATE_PASSED=true in .env"
echo "  4. Start paper mode: TRADING_MODE=paper in .env"
echo "  5. Start bot:       sudo systemctl start $SERVICE_NAME"
echo "  6. Check status:    sudo systemctl status $SERVICE_NAME"
echo "  7. View logs:       tail -f $BOT_DIR/logs/bot.log"
echo ""
echo "Useful commands:"
echo "  sudo systemctl stop $SERVICE_NAME"
echo "  sudo systemctl restart $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
