#!/bin/bash
# test-flow.sh — manual smoke test of the askpass client → daemon path.
#
# Calls the askpass binary with synthetic SUDO_* env vars. You should
# get a Telegram prompt; reply /yes_<id> or /no_<id>. Output:
#   APPROVED → password (or DRY_RUN_FAKE_PASSWORD) printed, exit 0
#   DENIED   → empty stdout, exit 1
#
# Does NOT actually invoke sudo. Safe to run with dry_run = true.

set -uo pipefail

SOCKET="${STG_SOCKET:-/run/sudo-telegram/sock}"

if [ ! -S "$SOCKET" ]; then
    echo "Socket $SOCKET not found. Is the daemon running?" >&2
    echo "  sudo systemctl status sudo-telegram" >&2
    exit 1
fi

echo "→ Sending test request via /usr/local/bin/sudo-telegram-askpass"
echo "→ Expect a Telegram message; respond /yes_<id> or /no_<id> within 60s"
echo

env -i \
    PATH="/usr/local/bin:/usr/bin" \
    SUDO_USER="$USER" \
    SUDO_COMMAND="/usr/bin/test --integration --from=$HOSTNAME" \
    PWD="$PWD" \
    USER="$USER" \
    /usr/local/bin/sudo-telegram-askpass

rc=$?
echo
echo "askpass exit code: $rc"
exit $rc
