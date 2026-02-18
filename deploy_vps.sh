#!/usr/bin/env bash
set -Eeuo pipefail

# Usage example:
# sudo APP_DIR=/opt/ramadan-bot RUN_USER=ubuntu SERVICE_NAME=ramadan-bot bash deploy_vps.sh

REPO_URL="${REPO_URL:-https://github.com/SultanbekKenesbaev/ram.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/ramadan-bot}"
SERVICE_NAME="${SERVICE_NAME:-ramadan-bot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_USER="${RUN_USER:-${SUDO_USER:-$USER}}"

log() { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Command not found: $1"
}

run_as_user() {
  if [[ "$RUN_USER" == "root" ]]; then
    "$@"
  else
    su -s /bin/bash "$RUN_USER" -c "$(printf '%q ' "$@")"
  fi
}

if [[ "$(id -u)" -ne 0 ]]; then
  fail "Run as root: sudo bash deploy_vps.sh"
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
  fail "User does not exist: $RUN_USER"
fi
RUN_GROUP="$(id -gn "$RUN_USER")"

if command -v apt-get >/dev/null 2>&1; then
  log "Installing system packages (git, python3, python3-venv)..."
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv
else
  log "apt-get not found, assuming dependencies are already installed."
  require_cmd git
  require_cmd "$PYTHON_BIN"
fi

log "Creating app directory: $APP_DIR"
install -d -m 755 "$APP_DIR"

if [[ -d "$APP_DIR/.git" ]]; then
  log "Repository exists, updating..."
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
  log "Cloning repository to $APP_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

chown -R "$RUN_USER:$RUN_GROUP" "$APP_DIR"

if [[ ! -d "$APP_DIR/.venv" ]]; then
  log "Creating virtual environment..."
  run_as_user "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi

log "Installing Python dependencies..."
run_as_user "$APP_DIR/.venv/bin/pip" install --upgrade pip
if [[ -f "$APP_DIR/requirements.txt" ]]; then
  run_as_user "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
else
  run_as_user "$APP_DIR/.venv/bin/pip" install aiogram python-dotenv
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  log "Creating .env template..."
  cat >"$APP_DIR/.env" <<'EOF'
BOT_TOKEN=
EOF
  chown "$RUN_USER:$RUN_GROUP" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

touch "$APP_DIR/time.txt" "$APP_DIR/molitva-saharlik.txt" "$APP_DIR/molitva-iftar.txt" "$APP_DIR/users.json"
chown "$RUN_USER:$RUN_GROUP" "$APP_DIR/time.txt" "$APP_DIR/molitva-saharlik.txt" "$APP_DIR/molitva-iftar.txt" "$APP_DIR/users.json"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
log "Writing systemd service: $SERVICE_FILE"
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Ramadan Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

log "Reloading systemd and starting service..."
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

log "Deployment finished."
log "Project path: $APP_DIR"
log "Service name: $SERVICE_NAME"
log "Service status: systemctl status $SERVICE_NAME --no-pager"
log "Logs: journalctl -u $SERVICE_NAME -f"
log "Edit token: nano $APP_DIR/.env"
