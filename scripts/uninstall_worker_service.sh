#!/bin/bash
# Stops and removes the launchd LaunchAgent installed by
# scripts/install_worker_service.sh. Does not touch logs/ or data/ — only
# the service registration.

set -euo pipefail

LABEL="com.retailassistant.worker"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$PLIST_PATH" ]; then
    echo "Not installed: $PLIST_PATH does not exist."
    exit 0
fi

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "Removed: $LABEL ($PLIST_PATH)"
