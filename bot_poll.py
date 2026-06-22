#!/usr/bin/env python3
"""
Conversational one-off stock checker for one8.com (Telegram).

You message your bot a product name (e.g. "seam xviii red" or "pavilion white") and it
searches one8.com, then replies with that product's current stock per size/colour.
One-off check only — it does NOT change what the 24/7 monitor watches.

How it runs: a scheduled GitHub Actions job (every 5 min) reads pending Telegram messages
via getUpdates, answers them, and stores the read-offset so nothing is processed twice.
So replies arrive within ~5 minutes — works from anywhere, with your Mac off.

Only messages from TELEGRAM_CHAT_ID (you) are answered; anything else is ignored.

Config: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, STORE_DOMAIN (default one8.com),
        OFFSET_FILE (default ./telegram_offset.json)

Test locally without touching Telegram updates:
    python3 bot_poll.py --query "seam xviii red"     # searches + sends you the reply
    python3 bot_poll.py --dry "seam xviii red"       # searches + prints reply, sends nothing
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

STORE_DOMAIN = os.environ.get("STORE_DOMAIN", "one8.com")
OFFSET_FILE = os.environ.get(
    "OFFSET_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_offset.json")
)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HTTP_TIMEOUT = 20
MAX_MATCHES = 4


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ------------------------------ http helpers --------------------------------

def http_json(url, data=None):
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_catalog():
    """All one8.com products (paginated). Returns list (may be empty on failure)."""
    products = []
    for page in range(1, 6):
        try:
            d = http_json(f"https://{STORE_DOMAIN}/products.json?limit=250&page={page}")
        except Exception as e:  # noqa: BLE001
            log(f"catalog page {page} failed: {e!r}")
            break
        items = d.get("products", [])
        if not items:
            break
        products += items
    return products


def fetch_product_js(handle):
    cb = int(time.time() * 1000)
    try:
        return http_json(f"https://{STORE_DOMAIN}/products/{handle}.js?_={cb}")
    except Exception as e:  # noqa: BLE001
        log(f"[{handle}] .js fetch failed: {e!r}")
        return None


# ------------------------------- search -------------------------------------

STOPWORDS = {"the", "a", "in", "stock", "shoe", "shoes", "size", "is", "available", "for", "of"}


def search_catalog(query, products):
    toks = [t for t in re.split(r"\W+", query.lower()) if t and t not in STOPWORDS]
    if not toks:
        return []
    scored = []
    for p in products:
        hay = (p.get("title", "") + " " + p.get("handle", "")).lower()
        score = sum(1 for t in toks if t in hay)
        if score:
            scored.append((score, p))
    if not scored:
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][0]
    return [p for s, p in scored if s == best][:MAX_MATCHES]


# ----------------------------- reply building -------------------------------

def _size_key(s):
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else 999


def price_str(price_minor):
    try:
        return f"₹{price_minor / 100:,.0f}"
    except Exception:  # noqa: BLE001
        return "price n/a"


def product_status_block(handle):
    """Authoritative status for one product via its .js. Returns a text block or None."""
    data = fetch_product_js(handle)
    if not data or "variants" not in data:
        return None
    title = data.get("title", handle)
    colour = ""
    price = None
    in_stock, sold_out = [], []
    for v in data["variants"]:
        colour = v.get("option1") or colour
        price = v.get("price") if price is None else price
        size = v.get("option2") or v.get("title", "")
        (in_stock if v.get("available") else sold_out).append(size)
    in_stock.sort(key=_size_key)
    url = f"https://{STORE_DOMAIN}/products/{handle}"

    lines = [f"👟 <b>{title}</b>"]
    if colour:
        lines.append(f"Colour: {colour}")
    if in_stock:
        lines.append(f"✅ In stock: <b>{', '.join(in_stock)}</b>")
    else:
        lines.append("❌ Sold out (all sizes)")
    lines.append(f"Price: {price_str(price)}")
    lines.append(f'🔗 <a href="{url}">View product</a>')
    return "\n".join(lines)


def build_reply(query):
    products = fetch_catalog()
    if not products:
        return "⚠️ Couldn't reach one8.com just now — try again in a minute."
    matches = search_catalog(query, products)
    if not matches:
        return (f"🔎 No one8.com product matched “{query}”.\n"
                "Try fewer / different words, e.g. “seam xviii red” or “pavilion white”.")
    blocks = []
    for p in matches:
        block = product_status_block(p["handle"])
        if block:
            blocks.append(block)
    if not blocks:
        return "⚠️ Found the product but couldn't read its stock just now — try again shortly."
    header = (f"🔎 Results for “{query}”"
              + (f" — {len(blocks)} matches:" if len(blocks) > 1 else ":"))
    return header + "\n\n" + "\n\n".join(blocks)


# ----------------------------- telegram i/o ---------------------------------

def tg(method, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    return http_json(url, data=data)


def send_message(text):
    try:
        body = tg("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=text,
                  parse_mode="HTML", disable_web_page_preview="true")
        if body.get("ok"):
            log("reply sent")
            return True
        log(f"sendMessage not ok: {body}")
    except Exception as e:  # noqa: BLE001
        log(f"sendMessage failed: {e!r}")
    return False


def load_offset():
    try:
        with open(OFFSET_FILE) as f:
            return int(json.load(f).get("offset", 0))
    except Exception:  # noqa: BLE001
        return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        json.dump({"offset": offset, "updated": datetime.now(timezone.utc).isoformat()}, f, indent=2)


HELP = ("👋 Send me any one8.com product name and I'll check its stock.\n"
        "Examples: “seam xviii red”, “pavilion white”, “sonic curve leggings”.\n"
        "(One-off check — your 24/7 shoe monitor keeps running separately.)")


def poll_once():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing — nothing to do")
        return 0
    offset = load_offset()
    try:
        updates = tg("getUpdates", offset=offset + 1, timeout=20, allowed_updates='["message"]')
    except Exception as e:  # noqa: BLE001
        log(f"getUpdates failed: {e!r}")
        return 0
    results = updates.get("result", [])
    log(f"{len(results)} new update(s)")
    max_id = offset
    handled = 0
    for u in results:
        max_id = max(max_id, u.get("update_id", offset))
        msg = u.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat_id != str(TELEGRAM_CHAT_ID):
            log(f"ignoring message from non-owner chat {chat_id}")
            continue
        if not text:
            continue
        if text.lower() in ("/start", "/help", "help"):
            send_message(HELP)
        else:
            query = re.sub(r"^/check\s+", "", text, flags=re.I).strip()
            log(f"checking: {query!r}")
            send_message(build_reply(query))
        handled += 1
    if max_id != offset:
        save_offset(max_id)
        log(f"offset advanced to {max_id}")
    log(f"handled {handled} message(s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--query":
        _reply = build_reply(sys.argv[2])
        print(_reply)
        send_message(_reply)
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--dry":
        print(build_reply(sys.argv[2]))
        sys.exit(0)
    sys.exit(poll_once())
