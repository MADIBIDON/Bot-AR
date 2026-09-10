#!/bin/bash
# Quick launchd-level status for the worker service — complements
# `python scripts/watch.py status` (which reports the app's own PID file
# and monitoring state) with what launchd itself thinks is running.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.retailassistant.worker"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$PLIST_PATH" ]; then
    echo "service:  not installed"
    exit 1
fi

echo "service:  installed ($PLIST_PATH)"

if launchctl list "$LABEL" >/dev/null 2>&1; then
    launchctl list "$LABEL"
else
    echo "launchd:  not loaded (run scripts/install_worker_service.sh)"
fi

echo
echo "recent log lines:"
tail -n 10 "$PROJECT_DIR/logs/worker.log" 2>/dev/null || echo "  (no logs/worker.log yet)"
