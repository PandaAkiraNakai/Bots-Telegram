#!/bin/bash
# get-chat-id.sh — print Telegram chat_ids that have messaged your bot.
#
# Usage:
#   1. Open Telegram, send /start (or any message) to your approval bot
#      from your personal account.
#   2. Run:  TOKEN=<bot-token> bash tools/get-chat-id.sh

set -euo pipefail

if [ -z "${TOKEN:-}" ]; then
    echo "Usage: TOKEN=<bot-token> bash $0" >&2
    exit 1
fi

JSON="$(curl -fsS "https://api.telegram.org/bot${TOKEN}/getUpdates")"

JSON="$JSON" python3 <<'PY'
import json, os, sys
data = json.loads(os.environ["JSON"])
if not data.get("ok"):
    print("Telegram API error:", data, file=sys.stderr)
    sys.exit(1)
seen = set()
for u in data["result"]:
    msg = u.get("message") or u.get("edited_message")
    if not msg:
        continue
    chat = msg.get("chat", {})
    cid = chat.get("id")
    if cid in seen:
        continue
    seen.add(cid)
    name = chat.get("first_name") or chat.get("title") or "?"
    ctype = chat.get("type")
    print(f"chat_id={cid}  type={ctype}  name={name}")
if not seen:
    print("No messages found. Send /start to your bot first.", file=sys.stderr)
    sys.exit(2)
PY
