#!/bin/bash
# INSTALL.sh — install sudo-telegram on the host.
#
# Run as root from the project directory:
#     sudo bash INSTALL.sh
#
# Idempotent: re-running upgrades the daemon/askpass binaries and leaves
# your config + password + sudoers whitelist untouched.

set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "INSTALL.sh must run as root" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_USER="${SUDO_USER:-sergioc}"

echo "==> Source: $SRC_DIR"
echo "==> Target user: $TARGET_USER"

# 1. Group for socket access
if ! getent group sudo-telegram >/dev/null; then
    echo "==> Creating group sudo-telegram"
    groupadd --system sudo-telegram
else
    echo "==> Group sudo-telegram exists"
fi

# 2. Add target user to the group (re-login required for it to take effect)
if id "$TARGET_USER" >/dev/null 2>&1; then
    if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx sudo-telegram; then
        echo "==> Adding $TARGET_USER to sudo-telegram group (re-login required)"
        usermod -a -G sudo-telegram "$TARGET_USER"
    else
        echo "==> $TARGET_USER already in sudo-telegram group"
    fi
fi

# 3. Directories
echo "==> Creating /etc/sudo-telegram, /var/log/sudo-telegram"
install -d -m 0750 -o root -g root /etc/sudo-telegram
install -d -m 0750 -o root -g sudo-telegram /var/log/sudo-telegram

# 4. Binaries (always overwrite — easy upgrade path)
echo "==> Installing /usr/local/bin/sudo-telegram-daemon"
install -m 0755 -o root -g root \
    "$SRC_DIR/bin/sudo-telegram-daemon.py" \
    /usr/local/bin/sudo-telegram-daemon

echo "==> Installing /usr/local/bin/sudo-telegram-askpass"
install -m 0755 -o root -g root \
    "$SRC_DIR/bin/sudo-telegram-askpass.py" \
    /usr/local/bin/sudo-telegram-askpass

# 5. systemd unit (always overwrite)
echo "==> Installing /etc/systemd/system/sudo-telegram.service"
install -m 0644 -o root -g root \
    "$SRC_DIR/config/sudo-telegram.service" \
    /etc/systemd/system/sudo-telegram.service

# 6. sudoers (only if missing — never overwrite a customised whitelist)
if [ ! -f /etc/sudoers.d/claude ]; then
    echo "==> Installing /etc/sudoers.d/claude"
    install -m 0440 -o root -g root \
        "$SRC_DIR/config/sudoers.d-claude" \
        /etc/sudoers.d/claude
    visudo -c -f /etc/sudoers.d/claude
else
    echo "==> /etc/sudoers.d/claude already present (leaving alone)"
fi

# 7. Profile.d — exports SUDO_ASKPASS for bash/zsh login shells
echo "==> Installing /etc/profile.d/sudo-telegram.sh"
cat > /etc/profile.d/sudo-telegram.sh <<'EOF'
# sudo-telegram: export SUDO_ASKPASS and alias sudo to use it.
export SUDO_ASKPASS=/usr/local/bin/sudo-telegram-askpass
alias sudo='sudo -A'
EOF
chmod 0644 /etc/profile.d/sudo-telegram.sh

# 7b. fish conf.d — fish does NOT read /etc/profile.d, so install equivalent.
if [ -d /etc/fish/conf.d ]; then
    echo "==> Installing /etc/fish/conf.d/sudo-telegram.fish"
    install -m 0644 -o root -g root \
        "$SRC_DIR/config/conf.d-sudo-telegram.fish" \
        /etc/fish/conf.d/sudo-telegram.fish
else
    echo "==> /etc/fish/conf.d not present (fish not system-wide), skipping"
fi

# 8. Config (only if missing)
if [ ! -f /etc/sudo-telegram/config.toml ]; then
    echo "==> Installing /etc/sudo-telegram/config.toml (EDIT BEFORE STARTING)"
    install -m 0400 -o root -g root \
        "$SRC_DIR/config/config.example.toml" \
        /etc/sudo-telegram/config.toml
else
    echo "==> /etc/sudo-telegram/config.toml already present (leaving alone)"
fi

# 9. Password file (only if missing — never overwrite)
if [ ! -f /etc/sudo-telegram/password ]; then
    echo "==> Creating empty /etc/sudo-telegram/password"
    install -m 0400 -o root -g root /dev/null /etc/sudo-telegram/password
else
    echo "==> /etc/sudo-telegram/password already present (leaving alone)"
fi

# 10. Audit log + chattr +a
LOG=/var/log/sudo-telegram/audit.log
if [ ! -f "$LOG" ]; then
    echo "==> Creating $LOG"
    install -m 0640 -o root -g sudo-telegram /dev/null "$LOG"
fi
if chattr +a "$LOG" 2>/dev/null; then
    echo "==> chattr +a applied to $LOG"
else
    echo "==> WARN: chattr +a failed (filesystem may not support it)" >&2
fi

# 11. Reload systemd
echo "==> systemctl daemon-reload"
systemctl daemon-reload

cat <<'POST'

──────────────────────────────────────────────────────────────────────
Installation complete.

Next steps:

  1. Edit /etc/sudo-telegram/config.toml
       sudo $EDITOR /etc/sudo-telegram/config.toml
     • approval_bot_token   (from BotFather)
     • chat_id              (TOKEN=... bash tools/get-chat-id.sh)
     • allowed_users        (e.g. ["sergioc"])

  2. Set the sudo password:
       printf '%s' 'YOUR-SUDO-PASS' | sudo tee /etc/sudo-telegram/password >/dev/null

  3. Start with dry-run first (set dry_run = true in config), then:
       sudo systemctl start sudo-telegram
       sudo journalctl -fu sudo-telegram
     Send /ping to your approval bot — should reply "pong".

  4. Test:  sudo -A whoami    # expect Telegram prompt; reply /yes_<id>
     With dry_run = true sudo will fail "incorrect password" — that is expected;
     it confirms the flow works without releasing the real password.

  5. Flip dry_run = false, restart, enable on boot:
       sudo systemctl restart sudo-telegram
       sudo systemctl enable sudo-telegram

  6. RE-LOGIN (or run `newgrp sudo-telegram`) so your shell picks up the
     sudo-telegram group + the SUDO_ASKPASS export from /etc/profile.d/.
──────────────────────────────────────────────────────────────────────
POST
