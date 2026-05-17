#!/bin/bash
# UNINSTALL.sh — remove sudo-telegram. Leaves /etc/sudo-telegram and
# /var/log/sudo-telegram for you to inspect/remove manually.

set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "UNINSTALL.sh must run as root" >&2
    exit 1
fi

echo "==> Stopping and disabling service"
systemctl disable --now sudo-telegram 2>/dev/null || true

echo "==> Removing binaries"
rm -f /usr/local/bin/sudo-telegram-daemon
rm -f /usr/local/bin/sudo-telegram-askpass

echo "==> Removing systemd unit"
rm -f /etc/systemd/system/sudo-telegram.service
systemctl daemon-reload

echo "==> Removing /etc/sudoers.d/claude"
rm -f /etc/sudoers.d/claude

echo "==> Removing /etc/profile.d/sudo-telegram.sh"
rm -f /etc/profile.d/sudo-telegram.sh

echo "==> Removing socket if present"
rm -f /run/sudo-telegram/sock

cat <<'POST'

Uninstall complete. NOT removed (do it manually if you want):

    /etc/sudo-telegram/         (config + password file)
    /var/log/sudo-telegram/     (audit log; chattr -a first to delete)
    sudo-telegram group         (groupdel sudo-telegram)

POST
