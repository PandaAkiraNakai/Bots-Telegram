#!/bin/bash
# INSTALL.sh — install bot-comandos-torre on the host.
#
# Run as root from the project directory:
#     sudo bash INSTALL.sh
#
# Idempotent: re-running upgrades el daemon, service unit y polkit rule.
# Deja /etc/bot-comandos-torre/config.toml intacto si ya existe.

set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "INSTALL.sh must run as root" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_USER="${SUDO_USER:-sergioc}"
TARGET_GROUP="$(id -gn "$TARGET_USER")"

echo "==> Source: $SRC_DIR"
echo "==> Target user: $TARGET_USER:$TARGET_GROUP"

# 1. Config dir (lo lee el daemon como TARGET_USER)
echo "==> Creating /etc/bot-comandos-torre"
install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" /etc/bot-comandos-torre

# 2. Daemon binary
echo "==> Installing /usr/local/bin/bot-comandos-torre"
install -m 0755 -o root -g root \
    "$SRC_DIR/bin/bot-comandos-torre.py" \
    /usr/local/bin/bot-comandos-torre

# 3. systemd unit
echo "==> Installing /etc/systemd/system/bot-comandos-torre.service"
install -m 0644 -o root -g root \
    "$SRC_DIR/config/bot-comandos-torre.service" \
    /etc/systemd/system/bot-comandos-torre.service

# 3b. Unidad oneshot pacman-update.service — la usa el botón "Aplicar"
# del menú /updates. El bot la arranca vía polkit (ver allowedUnits).
echo "==> Installing /etc/systemd/system/pacman-update.service"
install -m 0644 -o root -g root \
    "$SRC_DIR/config/pacman-update.service" \
    /etc/systemd/system/pacman-update.service

# 4. polkit rule (lets sergioc manage allow-listed power + service actions)
echo "==> Installing /etc/polkit-1/rules.d/50-bot-comandos-torre.rules"
install -m 0644 -o root -g root \
    "$SRC_DIR/config/50-bot-comandos-torre.rules" \
    /etc/polkit-1/rules.d/50-bot-comandos-torre.rules

# 5. State dir (SQLite + known_hosts)
echo "==> Creating /var/lib/bot-comandos-torre"
install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_GROUP" /var/lib/bot-comandos-torre

# 6. Log dir + audit.log con append-only
echo "==> Creating /var/log/bot-comandos-torre"
install -d -m 0750 -o "$TARGET_USER" -g "$TARGET_GROUP" /var/log/bot-comandos-torre
AUDIT="/var/log/bot-comandos-torre/audit.log"
if [ ! -f "$AUDIT" ]; then
    touch "$AUDIT"
    chown "$TARGET_USER:$TARGET_GROUP" "$AUDIT"
    chmod 0640 "$AUDIT"
fi
# Hazlo append-only — el daemon puede agregar pero no truncar ni borrar.
if command -v chattr >/dev/null 2>&1; then
    chattr +a "$AUDIT" 2>/dev/null || \
        echo "    (warn: no se pudo poner chattr +a en audit.log — sigue siendo writable)"
fi

# 7. Config (solo si falta — nunca pisa un token ya configurado)
if [ ! -f /etc/bot-comandos-torre/config.toml ]; then
    echo "==> Installing /etc/bot-comandos-torre/config.toml (EDIT BEFORE STARTING)"
    install -m 0400 -o "$TARGET_USER" -g "$TARGET_GROUP" \
        "$SRC_DIR/config/config.example.toml" \
        /etc/bot-comandos-torre/config.toml
else
    echo "==> /etc/bot-comandos-torre/config.toml already present (leaving alone)"
    echo "    (revisa config.example.toml para ver settings nuevos)"
fi

# 8. Reload systemd + polkit
echo "==> systemctl daemon-reload"
systemctl daemon-reload

# polkit picks up rules automatically (file watch); restart only if running.
if systemctl is-active --quiet polkit 2>/dev/null; then
    systemctl reload polkit 2>/dev/null || systemctl restart polkit 2>/dev/null || true
fi

# 8b. Validación cruzada: [services].manageable (TOML) ↔ allowedUnits (rule polkit).
# Si difieren, los botones de control de servicios van a fallar con "auth required".
# Solo se puede hacer post-config existente — la primera vez no tiene sentido.
if [ -f /etc/bot-comandos-torre/config.toml ] && command -v python3 >/dev/null 2>&1; then
    echo "==> Validating service whitelist cross-reference"
    python3 - <<'PY' || true
import re, sys, tomllib
try:
    cfg = tomllib.load(open('/etc/bot-comandos-torre/config.toml','rb'))
except Exception as e:
    print(f"  (warn: no pude leer config.toml: {e})", file=sys.stderr)
    sys.exit(0)
manageable = cfg.get('services', {}).get('manageable') or []
try:
    rule = open('/etc/polkit-1/rules.d/50-bot-comandos-torre.rules').read()
except OSError as e:
    print(f"  (warn: no pude leer la rule polkit: {e})", file=sys.stderr)
    sys.exit(0)
m = re.search(r'allowedUnits\s*=\s*\{([^}]*)\}', rule, re.DOTALL)
allowed = set(re.findall(r'"([^"]+)"\s*:\s*true', m.group(1))) if m else set()
expected = {(s if '.' in s else f'{s}.service') for s in manageable}
missing = sorted(expected - allowed)
extras = sorted(allowed - expected)
if missing:
    print(f"  WARN: services.manageable lista units NO autorizadas por la rule polkit (start/stop fallará): {', '.join(missing)}", file=sys.stderr)
if extras:
    print(f"  info: la rule polkit autoriza units no expuestas en services.manageable: {', '.join(extras)}", file=sys.stderr)
if not missing and not extras:
    print(f"  OK ({len(expected)} units alineadas)")
PY
fi

# 9. Sugerencias de paquetes opcionales
MISSING=()
command -v sensors      >/dev/null 2>&1 || MISSING+=("lm_sensors")
command -v smartctl     >/dev/null 2>&1 || MISSING+=("smartmontools (opcional)")
command -v checkupdates >/dev/null 2>&1 || MISSING+=("pacman-contrib (opcional, para /updates)")
python3 -c 'import matplotlib' 2>/dev/null || MISSING+=("python-matplotlib (opcional, para tendencias gráficas)")

cat <<POST

──────────────────────────────────────────────────────────────────────
Installation complete.

Próximos pasos:

  1. Edita /etc/bot-comandos-torre/config.toml — configura bot_token y
     opcionalmente [hosts.*], [smart], etc:
       sudo -u $TARGET_USER \$EDITOR /etc/bot-comandos-torre/config.toml

  2. Arranca:
       sudo systemctl start bot-comandos-torre
       sudo journalctl -fu bot-comandos-torre

  3. En Telegram, envíale /start o /menu al bot. Debe mostrar el menú.

  4. Si todo se ve bien:
       sudo systemctl enable bot-comandos-torre
POST

if [ "${#MISSING[@]}" -gt 0 ]; then
    echo ""
    echo "Paquetes recomendados / opcionales no detectados:"
    for p in "${MISSING[@]}"; do
        echo "  · $p"
    done
fi
echo "──────────────────────────────────────────────────────────────────────"
