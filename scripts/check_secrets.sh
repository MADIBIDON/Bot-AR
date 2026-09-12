#!/usr/bin/env bash
# Phase 26 audit (section 21): a lightweight, dependency-free secret scan
# over what's about to be committed — no external service, no false
# promise of being exhaustive. Reports only which staged file looks
# credential-shaped, never the matched value or even the line it's on
# (same "name the type, never the value" rule as market_data/ebay.py and
# notifications/discord/config.py already follow for runtime logging).
set -euo pipefail

pattern='(DISCORD_BOT_TOKEN|EBAY_CERT_ID|EBAY_APP_ID)[[:space:]]*=[[:space:]]*[^[:space:]=]|discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}'

flagged=""
while IFS= read -r file; do
    [ -z "$file" ] && continue
    case "$file" in
        *.lock) continue ;;
    esac
    if git show ":$file" 2>/dev/null \
        | grep -vE "fake|placeholder|example\.(com|test)" \
        | grep -qE "$pattern"; then
        flagged="$flagged $file"
    fi
done < <(git diff --cached --name-only --diff-filter=ACM)

if [ -n "$flagged" ]; then
    echo "check_secrets.sh: possible credential-shaped content staged in:" >&2
    for f in $flagged; do
        echo "  - $f" >&2
    done
    echo "Review manually before committing. No matched value is shown — only file names." >&2
    exit 1
fi

exit 0
