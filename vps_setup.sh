#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Alpha Engine — VPS Provisioning Script
# Target: Ubuntu 24.04 LTS (Noble Numbat)
# Run as root: sudo bash vps_setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BOT_DIR="/opt/alpha-engine"
BOT_USER="alphabot"
PYTHON_BIN=""           # resolved below
NODE_MAJOR=20

RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"; RESET="\033[0m"
info()  { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*"; exit 1; }

# ── 0. Sanity checks ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Run as root: sudo bash vps_setup.sh"

# Require Ubuntu 24.x
if [[ -f /etc/os-release ]]; then
    source /etc/os-release
    [[ "$ID" == "ubuntu" ]] || warn "Not Ubuntu — continuing but untested."
    [[ "${VERSION_ID}" == 24* ]] || warn "Expected Ubuntu 24.04; got ${VERSION_ID}. Continuing."
fi

# ── 1. System packages ─────────────────────────────────────────────────────────
info "Updating package lists..."
apt-get update -qq

info "Installing base system packages..."
apt-get install -y --no-install-recommends \
    curl wget gnupg ca-certificates lsb-release \
    software-properties-common build-essential \
    python3.12 python3.12-venv python3.12-dev \
    python3-pip \
    sqlite3 \
    rsync \
    git \
    fail2ban \
    ufw \
    unattended-upgrades apt-listchanges \
    logrotate \
    cron \
    systemd

# ── 2. Python 3.12 — ensure it's the default python3 ────────────────────────
PYTHON_BIN=$(command -v python3.12 || command -v python3)
info "Python binary: ${PYTHON_BIN} ($(${PYTHON_BIN} --version))"
[[ "${PYTHON_BIN}" == *"3.12"* || $(${PYTHON_BIN} -c "import sys;print(sys.version_info.minor)") -ge 12 ]] \
    || warn "Python 3.12 not confirmed — check manually."

# ── 3. Node.js 20 LTS ─────────────────────────────────────────────────────────
if ! command -v node &>/dev/null || [[ "$(node --version 2>/dev/null | cut -d. -f1 | tr -d 'v')" -lt ${NODE_MAJOR} ]]; then
    info "Installing Node.js ${NODE_MAJOR} LTS..."
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt-get install -y nodejs
else
    info "Node.js already at $(node --version) — skipping."
fi
info "Node: $(node --version)  npm: $(npm --version)"

# ── 4. UFW firewall — deny-all inbound, allow only SSH ───────────────────────
info "Configuring UFW firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 22/tcp
ufw --force enable
ufw status verbose
info "UFW: default-deny incoming, SSH allowed."

# ── 5. fail2ban ────────────────────────────────────────────────────────────────
info "Configuring fail2ban..."
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled  = true
port     = ssh
logpath  = %(sshd_log)s
EOF
systemctl enable fail2ban --now
systemctl restart fail2ban
info "fail2ban: sshd jail enabled (5 failures → 1h ban)."

# ── 6. Unattended security upgrades ───────────────────────────────────────────
info "Configuring unattended-upgrades for security patches..."
cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable unattended-upgrades --now
info "Automatic security updates enabled."

# ── 7. Create bot user ─────────────────────────────────────────────────────────
if ! id "${BOT_USER}" &>/dev/null; then
    info "Creating system user '${BOT_USER}'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "${BOT_USER}"
fi

# ── 8. Deploy bot code ─────────────────────────────────────────────────────────
info "Setting up bot directory at ${BOT_DIR}..."
mkdir -p "${BOT_DIR}"/{data,logs,backups}
rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
    "$(dirname "$0")/" "${BOT_DIR}/"
chown -R "${BOT_USER}:${BOT_USER}" "${BOT_DIR}"
chmod 750 "${BOT_DIR}"

# ── 9. Python virtual environment ─────────────────────────────────────────────
info "Creating Python 3.12 venv..."
"${PYTHON_BIN}" -m venv "${BOT_DIR}/.venv"
"${BOT_DIR}/.venv/bin/pip" install --quiet --upgrade pip wheel
"${BOT_DIR}/.venv/bin/pip" install --quiet -r "${BOT_DIR}/engine/requirements.txt"

# ── 10. Environment file (.env) ────────────────────────────────────────────────
ENV_FILE="${BOT_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    warn ".env not found — creating template at ${ENV_FILE}"
    cat > "${ENV_FILE}" << 'EOF'
# ── Solana ────────────────────────────────────────────────────────────────────
HELIUS_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_API_KEY
RPC_URL_FALLBACK=https://api.mainnet-beta.solana.com
SOLANA_NETWORK=devnet
SOLANA_WALLET_ADDRESS=YOUR_WALLET_PUBLIC_KEY
SOLANA_WALLET_PRIVATE_KEY=YOUR_WALLET_PRIVATE_KEY

# ── OSINT ─────────────────────────────────────────────────────────────────────
ARKHAM_API_KEY=YOUR_ARKHAM_API_KEY

# ── Telegram alerts ────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_TELEGRAM_CHAT_ID

# ── Engine ─────────────────────────────────────────────────────────────────────
ALPHA_LOG_LEVEL=INFO
ALPHA_LOG_DIR=/opt/alpha-engine/logs
ALPHA_DB_PATH=/opt/alpha-engine/data/alpha_engine.db
ALPHA_PID_FILE=/opt/alpha-engine/alpha_engine.pid
ALPHA_BOT_CMD=/opt/alpha-engine/.venv/bin/python -m engine.main
EOF
    warn "IMPORTANT: Edit ${ENV_FILE} before starting the services."
else
    info ".env found — skipping template creation."
fi
chmod 600 "${ENV_FILE}"
chown "${BOT_USER}:${BOT_USER}" "${ENV_FILE}"

# ── 11. systemd service: alpha-bot ────────────────────────────────────────────
info "Writing alpha-bot.service..."
cat > /etc/systemd/system/alpha-bot.service << EOF
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

LimitNOFILE=65536
MemoryMax=512M
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
EOF

# ── 12. systemd service: alpha-monitor ────────────────────────────────────────
info "Writing alpha-monitor.service..."
cat > /etc/systemd/system/alpha-monitor.service << EOF
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

# ── 13. logrotate — 30-day retention ──────────────────────────────────────────
info "Writing logrotate config (30-day retention)..."
cat > /etc/logrotate.d/alpha-engine << EOF
${BOT_DIR}/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    sharedscripts
    postrotate
        systemctl kill -s HUP alpha-bot.service 2>/dev/null || true
    endscript
}

${BOT_DIR}/logs/*.jsonl {
    weekly
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
}
EOF

# ── 14. Nightly SQLite backup cron job ────────────────────────────────────────
info "Setting up nightly SQLite backup (02:00 UTC)..."
CRON_FILE="/etc/cron.d/alpha-engine-backup"
cat > "${CRON_FILE}" << EOF
# Alpha Engine — nightly SQLite backup (retained 30 days)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

0 2 * * * ${BOT_USER} sqlite3 ${BOT_DIR}/data/alpha_engine.db ".backup ${BOT_DIR}/backups/alpha_engine_\$(date +\\%Y\\%m\\%d).db" && find ${BOT_DIR}/backups -name '*.db' -mtime +30 -delete
EOF
chmod 644 "${CRON_FILE}"
info "SQLite backups: ${BOT_DIR}/backups/ — retained 30 days."

# ── 15. Enable and reload everything ─────────────────────────────────────────
info "Enabling systemd services..."
systemctl daemon-reload
systemctl enable alpha-bot.service
systemctl enable alpha-monitor.service

# ── 16. Final status ───────────────────────────────────────────────────────────
echo ""
info "Provisioning complete on Ubuntu 24.04."
echo ""
echo "  Next steps:"
echo "  1. Edit   ${ENV_FILE}   (add your real keys)"
echo "  2. Run:"
echo "       sudo systemctl start alpha-bot.service"
echo "       sudo systemctl start alpha-monitor.service"
echo ""
echo "  Useful commands:"
echo "       sudo journalctl -fu alpha-bot      # live bot logs"
echo "       sudo journalctl -fu alpha-monitor  # live monitor logs"
echo "       sudo ufw status verbose             # firewall rules"
echo "       sudo fail2ban-client status sshd   # ban list"
echo "       ls ${BOT_DIR}/backups/             # SQLite backup files"
echo ""
warn "SECURITY: run on DEVNET (SOLANA_NETWORK=devnet) first and validate"
warn "before changing to mainnet. Never store private keys in version control."
echo ""
