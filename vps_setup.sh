#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Alpha Engine — VPS Setup Script
# Tested on Ubuntu 22.04 / Debian 12
# Run as root or with sudo.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BOT_DIR="/opt/alpha-engine"
BOT_USER="alphabot"
PYTHON_MIN="3.11"

RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; RESET="\033[0m"
info()  { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*"; exit 1; }

# ── 0. Sanity checks ─────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Run as root: sudo bash vps_setup.sh"
command -v python3 &>/dev/null || error "python3 not found. Install Python ${PYTHON_MIN}+."
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python version: ${PY_VER}"

# ── 1. System packages ────────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3-pip python3-venv python3-dev \
    build-essential libssl-dev libffi-dev \
    sqlite3 git curl wget \
    systemd logrotate

# ── 2. Create bot user (no login shell, no home) ─────────────────────────────
if ! id "${BOT_USER}" &>/dev/null; then
    info "Creating system user '${BOT_USER}'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "${BOT_USER}"
fi

# ── 3. Deploy bot code ────────────────────────────────────────────────────────
info "Setting up bot directory at ${BOT_DIR}..."
mkdir -p "${BOT_DIR}"/{data,logs}
rsync -a --exclude='.git' --exclude='__pycache__' \
    "$(dirname "$0")/" "${BOT_DIR}/"
chown -R "${BOT_USER}:${BOT_USER}" "${BOT_DIR}"

# ── 4. Python virtual environment ────────────────────────────────────────────
info "Creating Python venv..."
python3 -m venv "${BOT_DIR}/.venv"
"${BOT_DIR}/.venv/bin/pip" install --quiet --upgrade pip wheel
"${BOT_DIR}/.venv/bin/pip" install --quiet -r "${BOT_DIR}/engine/requirements.txt"

# ── 5. Environment file (.env) ───────────────────────────────────────────────
ENV_FILE="${BOT_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    warn ".env file not found — creating template at ${ENV_FILE}"
    cat > "${ENV_FILE}" <<'EOF'
# ── Solana ──────────────────────────────────────────────────────────────────
HELIUS_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_API_KEY
RPC_URL_FALLBACK=https://api.mainnet-beta.solana.com
SOLANA_NETWORK=devnet
SOLANA_WALLET_ADDRESS=YOUR_WALLET_PUBLIC_KEY
SOLANA_WALLET_PRIVATE_KEY=YOUR_WALLET_PRIVATE_KEY

# ── Telegram alerts ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_TELEGRAM_CHAT_ID

# ── Engine tuning ─────────────────────────────────────────────────────────────
ALPHA_LOG_LEVEL=INFO
ALPHA_LOG_DIR=/opt/alpha-engine/logs
ALPHA_DB_PATH=/opt/alpha-engine/data/alpha_engine.db
ALPHA_PID_FILE=/opt/alpha-engine/alpha_engine.pid
EOF
    warn "IMPORTANT: Edit ${ENV_FILE} before starting the services."
else
    info ".env found — skipping template creation."
fi
chmod 600 "${ENV_FILE}"
chown "${BOT_USER}:${BOT_USER}" "${ENV_FILE}"

# ── 6. systemd service: alpha-bot ────────────────────────────────────────────
info "Writing alpha-bot.service..."
cat > /etc/systemd/system/alpha-bot.service <<EOF
[Unit]
Description=Alpha Engine — SOL Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_USER}
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${BOT_DIR}/.venv/bin/python -m engine.main
Restart=on-failure
RestartSec=30s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=alpha-bot

# Safety limits
LimitNOFILE=65536
MemoryMax=512M
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
EOF

# ── 7. systemd service: alpha-monitor ────────────────────────────────────────
info "Writing alpha-monitor.service..."
cat > /etc/systemd/system/alpha-monitor.service <<EOF
[Unit]
Description=Alpha Engine — Heartbeat Monitor
After=network-online.target alpha-bot.service
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_USER}
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${BOT_DIR}/.venv/bin/python -m engine.infrastructure.monitor
Restart=always
RestartSec=15s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=alpha-monitor

LimitNOFILE=65536
MemoryMax=128M
TimeoutStartSec=30

[Install]
WantedBy=multi-user.target
EOF

# ── 8. logrotate config ───────────────────────────────────────────────────────
info "Writing logrotate config..."
cat > /etc/logrotate.d/alpha-engine <<EOF
${BOT_DIR}/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    sharedscripts
    postrotate
        systemctl kill -s HUP alpha-bot.service 2>/dev/null || true
    endscript
}

${BOT_DIR}/logs/*.jsonl {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
EOF

# ── 9. Enable and start ───────────────────────────────────────────────────────
info "Enabling systemd services..."
systemctl daemon-reload
systemctl enable alpha-bot.service
systemctl enable alpha-monitor.service

info "Done! Edit ${ENV_FILE} then run:"
echo ""
echo "  sudo systemctl start alpha-bot.service"
echo "  sudo systemctl start alpha-monitor.service"
echo ""
echo "  sudo journalctl -fu alpha-bot      # live bot logs"
echo "  sudo journalctl -fu alpha-monitor  # live monitor logs"
echo ""
warn "Run on DEVNET (SOLANA_NETWORK=devnet) first and validate before switching to mainnet."
