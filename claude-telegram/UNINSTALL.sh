#!/bin/bash
# UNINSTALL.sh — remove claude-telegram. Leaves /etc/claude-telegram for
# you to inspect/remove manually (it has your bot token).

set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "UNINSTALL.sh must run as root" >&2
    exit 1
fi

echo "==> Stopping and disabling service"
systemctl disable --now claude-telegram 2>/dev/null || true

echo "==> Removing binary"
rm -f /usr/local/bin/claude-telegram-daemon

echo "==> Removing systemd unit"
rm -f /etc/systemd/system/claude-telegram.service
systemctl daemon-reload

cat <<'POST'

Uninstall complete. NOT removed (do it manually if you want):

    /etc/claude-telegram/   (config + bot token)

POST
