#!/usr/bin/env python3
"""
One-time helper: print your Telegram chat ID.

Steps:
  1. Create a bot via @BotFather and copy its token.
  2. Open your bot in Telegram and send it any message (e.g. "hi").
  3. Run:  TELEGRAM_BOT_TOKEN=<token> python3 get_chat_id.py

It prints the chat ID(s) that have messaged the bot. Use that as TELEGRAM_CHAT_ID.
"""

import json
import os
import sys
import urllib.request

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    print("Set TELEGRAM_BOT_TOKEN first, e.g.:")
    print("  TELEGRAM_BOT_TOKEN=123:abc python3 get_chat_id.py")
    sys.exit(1)

url = f"https://api.telegram.org/bot{token}/getUpdates"
try:
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
except Exception as e:  # noqa: BLE001
    print(f"Request failed: {e!r}")
    sys.exit(1)

if not data.get("ok"):
    print(f"Telegram API error: {data}")
    sys.exit(1)

updates = data.get("result", [])
if not updates:
    print("No messages found. Send your bot a message in Telegram first, then re-run.")
    sys.exit(1)

seen = {}
for u in updates:
    msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
    chat = msg.get("chat", {})
    if "id" in chat:
        name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
        seen[chat["id"]] = name

if not seen:
    print("Got updates but no chat id — send a plain text message to the bot and re-run.")
    sys.exit(1)

print("Found chat id(s):")
for cid, name in seen.items():
    print(f"  TELEGRAM_CHAT_ID={cid}   (chat: {name})")
