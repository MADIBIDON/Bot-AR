#!/bin/bash
# Installs the monitoring worker as a per-user launchd service (LaunchAgent)
# so it runs at login and restarts after a crash, without a terminal open.
#
# No root required: this only touches ~/Library/LaunchAgents, the
# user-level launchd domain. The plist is generated here, at install time,
# so it always points at this exact checkout's absolute path and its
# .venv's python — nothing is hardcoded or shared across machines.
#
# Safe to re-run: an existing installation is unloaded and replaced.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
LABEL="com.retailassistant.worker"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$PROJECT_DIR/logs"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "No .venv found at: $PYTHON_BIN" >&2
    echo "Create it first, e.g.: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "Warning: no .env found at $PROJECT_DIR/.env — the worker will fail to start" >&2
    echo "         until DISCORD_BOT_TOKEN etc. are set there." >&2
fi

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$PLIST_PATH")"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>-m</string>
        <string>app.main_worker</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/launchd-stderr.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST

# Re-installable: drop any existing instance of this label first (errors
# ignored — it may simply not be loaded yet).
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load -w "$PLIST_PATH"

echo "Installed and started: $LABEL"
echo "  plist:   $PLIST_PATH"
echo "  worker:  $PYTHON_BIN -m app.main_worker"
echo "  cwd:     $PROJECT_DIR"
echo "  logs:    $LOG_DIR/worker.log (app logs), $LOG_DIR/launchd-std{out,err}.log (process-level)"
echo
echo "Check status:   scripts/worker_status.sh"
echo "Remove service: scripts/uninstall_worker_service.sh"
