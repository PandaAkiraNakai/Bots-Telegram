#!/bin/bash
# UNINSTALL.sh — remove bot-comandos-torre.
#
# Stops + disables the service, removes the binary, unit, and polkit rule.
# Deja /etc/bot-comandos-torre/, /var/lib/bot-comandos-torre/ y
# /var/log/bot-comandos-torre/ para que los inspecciones / borres a mano.

set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "UNINSTALL.sh must run as root" >&2
    exit 1
fi

echo "==> Stopping + disabling bot-comandos-torre"
systemctl disable --now bot-comandos-torre 2>/dev/null || true

echo "==> Removing binary, unit, polkit rule"
rm -f /usr/local/bin/bot-comandos-torre
rm -f /etc/systemd/system/bot-comandos-torre.service
rm -f /etc/polkit-1/rules.d/50-bot-comandos-torre.rules

echo "==> systemctl daemon-reload"
systemctl daemon-reload

cat <<'POST'

Hecho. Quedan intactos:
  · /etc/bot-comandos-torre/        (config + token)
  · /var/lib/bot-comandos-torre/    (SQLite + known_hosts)
  · /var/log/bot-comandos-torre/    (audit.log con chattr +a)

Para borrarlos manualmente:

    sudo chattr -a /var/log/bot-comandos-torre/audit.log 2>/dev/null
    sudo rm -rf /etc/bot-comandos-torre /var/lib/bot-comandos-torre /var/log/bot-comandos-torre

POST
