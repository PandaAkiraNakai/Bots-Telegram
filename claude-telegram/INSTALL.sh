#!/bin/bash
# INSTALL.sh — install claude-telegram on the host.
#
# Run as root from the project directory:
#     sudo bash INSTALL.sh
#
# Idempotent: re-running upgrades the daemon binary and leaves your
# config + token untouched.

set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "INSTALL.sh must run as root" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_USER="${SUDO_USER:-sergioc}"

echo "==> Source: $SRC_DIR"
echo "==> Target user: $TARGET_USER"

# 1. Config dir owned by sergioc (daemon runs as sergioc and reads it)
echo "==> Creating /etc/claude-telegram"
install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_USER" /etc/claude-telegram

# 2. Daemon binary
echo "==> Installing /usr/local/bin/claude-telegram-daemon"
install -m 0755 -o root -g root \
    "$SRC_DIR/bin/claude-telegram-daemon.py" \
    /usr/local/bin/claude-telegram-daemon

# 2b. Python venv with faster-whisper for voice transcription.
#     Lives at /opt/claude-telegram/venv (root-owned) and is what the
#     systemd unit invokes. Re-running this is idempotent.
VENV=/opt/claude-telegram/venv
if [ ! -x "$VENV/bin/python" ]; then
    echo "==> Creating venv at $VENV"
    install -d -m 0755 -o root -g root /opt/claude-telegram
    python3 -m venv "$VENV"
fi
echo "==> Ensuring faster-whisper installed in venv (this can take a minute)"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade faster-whisper

# 3. systemd unit
echo "==> Installing /etc/systemd/system/claude-telegram.service"
install -m 0644 -o root -g root \
    "$SRC_DIR/config/claude-telegram.service" \
    /etc/systemd/system/claude-telegram.service

# 4. Config (only if missing)
if [ ! -f /etc/claude-telegram/config.toml ]; then
    echo "==> Installing /etc/claude-telegram/config.toml (EDIT BEFORE STARTING)"
    install -m 0400 -o "$TARGET_USER" -g "$TARGET_USER" \
        "$SRC_DIR/config/config.example.toml" \
        /etc/claude-telegram/config.toml
else
    echo "==> /etc/claude-telegram/config.toml already present (leaving alone)"
fi

# 5. Reload systemd
echo "==> systemctl daemon-reload"
systemctl daemon-reload

cat <<'POST'

──────────────────────────────────────────────────────────────────────
Installation complete.

Next steps:

  1. Create the bot in @BotFather (separate from sudo bot!), and grab
     the token. Send /start to it from your phone.

  2. Get your chat_id:
       TOKEN=<bot-token> bash tools/get-chat-id.sh

  3. Edit the config (as sergioc, since the file is owned by you):
       $EDITOR /etc/claude-telegram/config.toml
     Set bot_token and chat_id. Optionally tweak working_dir / extra_args.

  4. Start it:
       sudo systemctl start claude-telegram
       sudo journalctl -fu claude-telegram

     Send /ping to your bot — should reply "pong".
     Then send any message — it goes to `claude -p`.

  5. Enable on boot:
       sudo systemctl enable claude-telegram
──────────────────────────────────────────────────────────────────────
POST
