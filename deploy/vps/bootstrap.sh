#!/bin/bash
# Prepares an Ubuntu 22.04/24.04 server to run the worker 24/7, then
# installs it as a systemd service. Run ON THE SERVER, as a sudo-capable
# user, after the project has been copied to /opt/bot-ar (see README.md).
#
# Safe to re-run: every step is idempotent.

set -euo pipefail

APP_DIR="/opt/bot-ar"
APP_USER="botar"
SERVICE="retail-worker"

if [ ! -f "$APP_DIR/pyproject.toml" ]; then
    echo "Project not found in $APP_DIR — copy it there first (README.md, step 3)." >&2
    exit 1
fi
if [ ! -f "$APP_DIR/.env" ]; then
    echo "No $APP_DIR/.env — copy your .env there first (README.md, step 3)." >&2
    exit 1
fi

sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip sqlite3

if ! id "$APP_USER" >/dev/null 2>&1; then
    sudo useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

sudo mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
sudo chmod 600 "$APP_DIR/.env"

sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

sudo cp "$APP_DIR/deploy/vps/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"

echo
echo "Installed. Useful commands:"
echo "  sudo systemctl status $SERVICE"
echo "  sudo journalctl -u $SERVICE -f"
echo "  sudo -u $APP_USER $APP_DIR/.venv/bin/python -m app.healthcheck"
