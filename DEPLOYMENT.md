# Bybit Futures Bot — Deployment Guide

## Overview

Complete setup guide covering:
1. [Bybit API Key Setup (with Hedge Mode)](#1-bybit-api-key-setup)
2. [Oracle Cloud Always-Free Instance](#2-oracle-cloud-always-free)
3. [Bot Installation & Configuration](#3-bot-installation)
4. [Running the Backtest (quality gate)](#4-backtest--quality-gate)
5. [Running the Bot (paper → live)](#5-running-the-bot)
6. [Process Management (systemd)](#6-systemd-service)
7. [Monitoring & Slack Alerts](#7-monitoring)

---

## 1. Bybit API Key Setup

### 1-A. Enable Hedge Mode on your account

Hedge Mode lets the bot hold **both long and short positions simultaneously** on the same symbol, which is required for this bot's default configuration (`BYBIT_HEDGE_MODE=true`).

1. Log in to [bybit.com](https://www.bybit.com)
2. Go to **Derivatives → USDT Perpetual** (or any futures market)
3. Click the **gear icon (⚙)** in the top-right corner of the trading interface
4. Under **Position Mode** → select **"Hedge Mode"** → confirm
5. *(Alternative path: Account → Settings → Derivatives → Position Mode → Hedge Mode)*

> **Note:** Once Hedge Mode is enabled, the bot's `positionIdx=1` (long) / `positionIdx=2` (short) calls will work correctly. If you prefer One-Way Mode, set `BYBIT_HEDGE_MODE=false` in `.env`.

### 1-B. Create API Keys

1. Log in to Bybit → **Account** (top-right) → **API Management**
2. Click **"Create New Key"** → select **"System-generated API Keys"**
3. **API Key Name**: `futures-bot` (or any name you choose)
4. **Permissions** — enable ONLY the following (do NOT enable Withdrawal):
   - ✅ Read-Write → **Trade** (place/cancel orders)
   - ✅ Read-Write → **Unified Account** (read balances/positions)
   - ✅ Read-Write → **Derivatives** (futures order access)
   - ❌ **Asset / Withdrawal** — NEVER enable this
5. **IP Whitelist** (recommended): Enter your Oracle Cloud server's public IP
   - Find your IP after creating the Oracle VM (step 2 below)
   - Format: single IP like `152.x.x.x` or CIDR `152.x.x.x/32`
6. Click **Submit** → enter 2FA code
7. **Save your `API Key` and `API Secret` immediately** — the secret is shown only once

> **Testnet keys**: For testing, create keys at [testnet.bybit.com](https://testnet.bybit.com). Set `BYBIT_TESTNET=true` in `.env`.

---

## 2. Oracle Cloud Always-Free

Oracle offers **always-free** VMs — no credit card charge after initial signup verification.

### 2-A. Create an Oracle Cloud account

1. Go to [cloud.oracle.com](https://cloud.oracle.com) → **Start for Free**
2. Choose home region: **South Korea Central (Seoul)**, **Japan East (Tokyo)**, or **US East (Ashburn)**
   > ⚠ The ARM A1 (best free tier) fills up quickly. Seoul, Singapore, and US East are more reliable.
3. Complete signup (credit card required for verification, **not charged**)
4. Wait for account activation (usually 5–15 minutes)

### 2-B. Create an ARM A1 VM (Recommended)

The **A1.Flex** ARM instance gives 4 OCPUs + 24 GB RAM — always free.

1. Log in to Oracle Cloud → **Compute** → **Instances** → **Create Instance**
2. **Name**: `bybit-bot`
3. **Image**: Ubuntu 22.04 LTS (Minimal) — click "Change Image" if needed
4. **Shape**: Click **"Change Shape"** → Ampere → **VM.Standard.A1.Flex**
   - Set OCPU: **2** (or 4 max free)
   - Memory: **12 GB** (or 24 GB max free)
5. **Network**: Accept defaults (creates a VCN automatically)
6. **SSH Keys**: Upload your public key (`~/.ssh/id_rsa.pub`)
   - If you don't have one: `ssh-keygen -t ed25519 -C "bybit-bot"`
7. **Boot volume**: 50 GB (the 200 GB maximum is free)
8. Click **Create**

Wait ~2 minutes for provisioning. Note the **Public IP Address** shown in instance details.

### 2-C. Configure firewall (Security List)

By default, Oracle blocks inbound connections except SSH (port 22). The bot only needs outbound access (to Bybit API), so no additional ports need opening.

Optionally, allow SSH only from your home IP:
1. VCN → Security Lists → Default Security List → Edit Ingress Rules
2. Change SSH rule source from `0.0.0.0/0` to your home IP

### 2-D. Connect via SSH

```bash
ssh ubuntu@<YOUR_ORACLE_IP>
```

---

## 3. Bot Installation

Run all commands on the Oracle VM (after SSH connection).

### 3-A. Install system dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3.11-dev \
    python3-pip git sqlite3 screen
```

Verify Python version:
```bash
python3.11 --version   # should show Python 3.11.x
```

### 3-B. Upload or clone the bot code

**Option A — Git (if you push to GitHub):**
```bash
git clone https://github.com/YOUR_USERNAME/bybit-futures-bot.git
cd bybit-futures-bot
```

**Option B — SCP from local machine:**
```bash
# Run on your local machine (not the server):
scp -r /path/to/bybit-futures-bot ubuntu@<ORACLE_IP>:~/
```

### 3-C. Create virtual environment and install dependencies

```bash
cd ~/bybit-futures-bot
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3-D. Configure the bot

```bash
cp .env.example .env
nano .env    # or use: vi .env
```

Fill in ALL values marked below:

```ini
# ── Required: Fill these in ──────────────────────────────────
TRADING_MODE=paper             # Start with paper! Switch to live only after QG passes

BYBIT_API_KEY=your_api_key_here         # From step 1-B
BYBIT_API_SECRET=your_api_secret_here   # From step 1-B
BYBIT_TESTNET=false            # false = mainnet (real data), true = testnet
BYBIT_HEDGE_MODE=true          # Must match your Bybit account position mode setting

# ── Capital ──────────────────────────────────────────────────
PAPER_CAPITAL_USDT=10000.0     # Virtual starting capital for paper trading

# ── Optional: Slack notifications ────────────────────────────
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# ── Quality Gate (set true ONLY after backtest passes) ───────
QUALITY_GATE_PASSED=false
```

Save and exit (`Ctrl+X → Y → Enter` in nano).

### 3-E. Initialize the database

```bash
source venv/bin/activate
python -c "from src.db.database import Database; from src.config import settings; Database(settings)"
echo "Database initialized"
```

### 3-F. Test configuration

```bash
# Dry-run test (no real orders, no API calls)
TRADING_MODE=dry-run python -m src.main &
sleep 10
kill %1
```

Check logs:
```bash
tail -f logs/bot.log
```

If you see `TradingBot ready | mode=dry-run` — the configuration is correct.

---

## 4. Backtest & Quality Gate

> ⚠ **IMPORTANT**: The quality gate **must pass on real historical data** before enabling live trading. The synthetic backtest validates pipeline mechanics only — it does NOT produce reliable quality gate results.

### 4-A. Quick pipeline test (synthetic data, no API required)

```bash
source venv/bin/activate
python scripts/backtest_btc.py --days 90 --quiet
```

Expected output: 50+ trades generated, pipeline running correctly. The quality gate will typically FAIL on synthetic data (GBM has no market microstructure edge) — this is normal.

### 4-B. Real historical backtest (requires API keys)

```bash
source venv/bin/activate
python -m src.backtest.engine \
    --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT \
    --years 2 \
    --interval 5
```

This downloads 2 years of 5-minute data from Bybit (~580k bars per symbol) and runs the full backtest.

Expected runtime: 10–30 minutes depending on server speed.

**Quality Gate thresholds** (all must pass):
| Metric | Required | Meaning |
|--------|----------|---------|
| Sharpe Ratio | ≥ 1.0 | Risk-adjusted return is acceptable |
| Max Drawdown | ≤ 30% | Strategy doesn't blow up |
| Win Rate | ≥ 45% | More than random |
| Profit Factor | ≥ 1.5 | Wins exceed losses by 50% |
| Min Trades | ≥ 100 | Statistically significant sample |

### 4-C. Enable live trading after quality gate passes

**Only after the backtest passes ALL quality gate criteria:**

```bash
nano .env
# Change:
QUALITY_GATE_PASSED=true
TRADING_MODE=live
```

> The bot enforces this: `settings.check_live_allowed()` will raise an error and refuse to start in LIVE mode unless `QUALITY_GATE_PASSED=true`.

---

## 5. Running the Bot

### 5-A. Paper trading (recommended first step)

Run with `TRADING_MODE=paper` to trade with real market signals but fake capital:

```bash
source venv/bin/activate
TRADING_MODE=paper python -m src.main
```

Monitor paper performance for 2–4 weeks. Compare results to the backtest expectations.

### 5-B. Live trading

After paper trading confirms expected behaviour:

```bash
source venv/bin/activate
python -m src.main   # reads TRADING_MODE from .env
```

---

## 6. systemd Service

Run the bot as a system service that auto-starts on reboot.

### 6-A. Create the service file

```bash
sudo nano /etc/systemd/system/bybit-bot.service
```

Paste:
```ini
[Unit]
Description=Bybit Futures Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/bybit-futures-bot
ExecStart=/home/ubuntu/bybit-futures-bot/venv/bin/python -m src.main
Restart=on-failure
RestartSec=30
KillMode=process
StandardOutput=append:/home/ubuntu/bybit-futures-bot/logs/bot.log
StandardError=append:/home/ubuntu/bybit-futures-bot/logs/bot.log

[Install]
WantedBy=multi-user.target
```

### 6-B. Enable and start the service

```bash
# Create logs directory
mkdir -p ~/bybit-futures-bot/logs

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable bybit-bot
sudo systemctl start bybit-bot

# Check status
sudo systemctl status bybit-bot
```

### 6-C. Service management commands

```bash
sudo systemctl stop bybit-bot      # Stop the bot
sudo systemctl restart bybit-bot   # Restart (e.g., after .env changes)
sudo systemctl status bybit-bot    # Check running status
journalctl -u bybit-bot -f         # Follow live logs (systemd journal)
tail -f ~/bybit-futures-bot/logs/bot.log  # Follow file-based logs
```

---

## 7. Monitoring

### 7-A. Slack notifications

Set `SLACK_WEBHOOK_URL` in `.env`:
1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. App Name: `Bybit Bot` | Workspace: your Slack workspace
3. **Incoming Webhooks** → Turn On → **Add New Webhook to Workspace**
4. Select channel (e.g., `#trading-bot`) → **Allow**
5. Copy the **Webhook URL** → paste into `.env`

The bot sends alerts for:
- 🟢 Bot startup / shutdown
- 📊 Order placed / filled
- ⚠️ Risk guard triggered (HALT/PAUSE)
- ❌ Circuit breaker opened
- 📈 Daily briefing (9 AM KST)

### 7-B. Log rotation

Prevent logs from filling the disk:
```bash
sudo nano /etc/logrotate.d/bybit-bot
```

```
/home/ubuntu/bybit-futures-bot/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

### 7-C. Disk space monitoring

```bash
# Check disk usage
df -h

# Check bot process memory
ps aux | grep "src.main"
```

---

## 8. Complete Step-by-Step Checklist

```
□ 1. Log in to Bybit → enable Hedge Mode
□ 2. Create Bybit API key (Trade + Read, NO Withdrawal, IP whitelist)
□ 3. Create Oracle Cloud account (Seoul or US East region)
□ 4. Create ARM A1.Flex VM (Ubuntu 22.04, 2 OCPU, 12 GB RAM)
□ 5. SSH into VM
□ 6. sudo apt update && install Python 3.11 + git
□ 7. Upload/clone bot code to ~/bybit-futures-bot
□ 8. python3.11 -m venv venv && pip install -r requirements.txt
□ 9. cp .env.example .env → fill in API keys
□ 10. python scripts/backtest_btc.py  (pipeline check, synthetic)
□ 11. python -m src.backtest.engine --years 2  (REAL data quality gate)
□ 12. If QG passes: set QUALITY_GATE_PASSED=true in .env
□ 13. Set TRADING_MODE=paper → run for 2-4 weeks
□ 14. Review paper trading results vs backtest expectations
□ 15. If satisfactory: set TRADING_MODE=live
□ 16. Set up systemd service (auto-restart)
□ 17. Configure Slack webhook for alerts
□ 18. Set up log rotation
```

---

## 9. Security Reminders

- **Never commit `.env` to git** — it's in `.gitignore`
- **API key permissions**: Trade + Read only. NEVER enable Withdrawal.
- **IP whitelist** your Bybit API key to the Oracle server's IP
- **Rotate API keys** every 90 days
- Use `BYBIT_TESTNET=true` for testing before going live
- The bot enforces `QUALITY_GATE_PASSED=true` before allowing live mode

See `SECURITY.md` for full details on secret management.

---

## 10. Troubleshooting

| Problem | Solution |
|---------|----------|
| `QUALITY_GATE_PASSED must be true` | Run backtest first, then set flag |
| `BybitAPIError [10003]` | Wrong API key or secret |
| `BybitAPIError [110001]` | Symbol not found — check symbol name |
| `BybitAPIError [110043]` | Leverage already set (harmless) |
| `BybitAPIError [110025]` | Position mode already set (harmless) |
| `BybitAPIError [30032]` | Hedge mode mismatch — set `BYBIT_HEDGE_MODE` correctly |
| `Circuit breaker OPEN` | 5 consecutive API failures — check network/API key |
| Bot not generating signals | Lower `SIGNAL_ENTRY_THRESHOLD` (e.g., 0.45) |
| Low trade frequency | Increase `TOP_N_COINS` or lower `SIGNAL_FILTER_ER_THRESHOLD` |
