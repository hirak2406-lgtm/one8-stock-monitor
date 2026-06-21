#!/usr/bin/env python3
"""
Stock monitor for one8.com — Shopify availability watcher.

Watches the **Seam XVIII Signature (men's)** sneaker across BOTH colourways and ALL
sizes, and sends a clean Telegram push the moment any variant comes back in stock.

Reliability model ("never miss / never false-alert / tell me if it breaks"):
  - Source of truth = Shopify's own `available` flag from each product `.js` endpoint.
  - Track every variant by ID; cache-buster + a confirming second fetch before alerting.
  - Transient fetch errors => keep prior state, no alert (self-heals next cycle).
  - SUSTAINED inability to read a product (e.g. handle renamed, ~30 min of failures, or a
    404) => sends a "⚠️ monitor degraded" warning so a silent break can't go unnoticed.
  - DAILY heartbeat ("✅ still watching") => if that ping ever stops, you know the bot died.
    Its once-a-day state commit also keeps the GitHub cron from auto-disabling.
  - Any unexpected crash exits non-zero => the workflow's failure step pings you (backstop).

Configuration (env vars override defaults):
  PRODUCT_HANDLES     Comma-separated Shopify handles (default: white + red Signature)
  STORE_DOMAIN        Store domain (default: one8.com)
  STATE_FILE          Path to state file (default: ./state.json)
  HEARTBEAT_UTC_HOUR  Hour (UTC) to send the daily heartbeat (default: 4 = ~9:30 IST)
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   Telegram credentials (required to send)

Flags:
  --test    Send a test Telegram message and exit (verifies your credentials).
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------- configuration --------------------------------

STORE_DOMAIN = os.environ.get("STORE_DOMAIN", "one8.com")

DEFAULT_HANDLES = [
    "seam-xviii-signature-mens-white",   # Classic White - Green Dew
    "seam-xviii-signature-mens-red",     # Test Red - Pitch Brown
]
PRODUCT_HANDLES = [
    h.strip() for h in os.environ.get("PRODUCT_HANDLES", ",".join(DEFAULT_HANDLES)).split(",")
    if h.strip()
]

STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
)
HEARTBEAT_UTC_HOUR = int(os.environ.get("HEARTBEAT_UTC_HOUR", "4"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HTTP_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF = 3

# How many consecutive cycles of failing to read a product before we warn.
# Cron runs every 5 min, so 6 ≈ 30 minutes of sustained trouble.
DEGRADE_THRESHOLD = 6


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ------------------------------ networking ----------------------------------

def fetch_product(handle):
    """Returns (data|None, status) where status in {'ok','notfound','error'}."""
    cache_buster = int(time.time() * 1000)
    url = f"https://{STORE_DOMAIN}/products/{handle}.js?_={cache_buster}"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return json.loads(resp.read().decode("utf-8")), "ok"
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                # Hard "gone" — retrying won't help. Likely the handle was renamed.
                log(f"[{handle}] HTTP {e.code} — product not found (handle changed/removed?)")
                return None, "notfound"
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        log(f"[{handle}] fetch attempt {attempt}/{MAX_RETRIES} failed: {last_err!r}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    log(f"[{handle}] could not read this cycle (last error: {last_err!r}) — prior state kept")
    return None, "error"


def scan_products():
    """
    Returns (seen, statuses):
      seen     = { variant_id: {available, handle, product_title, colour, size, price} }
      statuses = { handle: 'ok'|'notfound'|'error' }
    """
    seen = {}
    statuses = {}
    for handle in PRODUCT_HANDLES:
        data, status = fetch_product(handle)
        statuses[handle] = status
        if data and "variants" in data:
            for v in data["variants"]:
                vid = v.get("id")
                if vid is None:
                    continue
                seen[vid] = {
                    "available": bool(v.get("available")),
                    "handle": handle,
                    "product_title": data.get("title", handle),
                    "colour": v.get("option1") or "",
                    "size": v.get("option2") or v.get("title", ""),
                    "price": v.get("price"),
                }
    return seen, statuses


# -------------------------------- state -------------------------------------

DEFAULT_STATE = {"variants": {}, "fail_streak": 0, "last_heartbeat_date": None}


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return dict(DEFAULT_STATE)
    except Exception as e:  # noqa: BLE001
        log(f"state read failed ({e!r}); starting from defaults")
        return dict(DEFAULT_STATE)
    out = dict(DEFAULT_STATE)
    out["fail_streak"] = int(data.get("fail_streak", 0) or 0)
    out["last_heartbeat_date"] = data.get("last_heartbeat_date")
    variants = {}
    for k, val in (data.get("variants") or {}).items():
        try:
            variants[int(k)] = bool(val)
        except (ValueError, TypeError):
            continue
    out["variants"] = variants
    return out


def state_signature(state):
    """The fields whose change should trigger a (rare) commit — excludes last_checked."""
    return (
        tuple(sorted(state["variants"].items())),
        state["fail_streak"],
        state["last_heartbeat_date"],
    )


def write_state(state):
    payload = {
        "variants": {str(k): bool(v) for k, v in sorted(state["variants"].items())},
        "fail_streak": state["fail_streak"],
        "last_heartbeat_date": state["last_heartbeat_date"],
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(payload, f, indent=2)


# ----------------------------- notifications --------------------------------

def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — cannot send Telegram message")
        return False
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(api, data=payload)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                log("Telegram message sent")
                return True
            raise RuntimeError(body)
        except Exception as e:  # noqa: BLE001
            log(f"telegram attempt {attempt}/{MAX_RETRIES} failed: {e!r}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    log("Telegram send FAILED after retries")
    return False


def _size_key(size):
    m = re.search(r"\d+", size or "")
    return int(m.group()) if m else 999


def price_str(price_minor):
    try:
        return f"₹{price_minor / 100:,.0f}"
    except Exception:  # noqa: BLE001
        return "price n/a"


def build_restock_message(restocked):
    groups = {}
    for info in restocked:
        groups.setdefault(info["handle"], []).append(info)

    lines = ["🟢 <b>BACK IN STOCK</b> · one8.com", ""]
    for handle, items in groups.items():
        title = items[0]["product_title"]
        colour = items[0]["colour"]
        price = price_str(items[0]["price"])
        sizes = sorted({i["size"] for i in items}, key=_size_key)
        url = f"https://{STORE_DOMAIN}/products/{handle}"
        lines.append(f"👟 <b>{title}</b>")
        if colour:
            lines.append(f"Colour: {colour}")
        lines.append(f"Sizes available: <b>{', '.join(sizes)}</b>")
        lines.append(f"Price: {price}")
        lines.append(f'🔗 <a href="{url}">View product</a>')
        lines.append("")
    lines.append(f"<i>Detected {datetime.now(timezone.utc).strftime('%H:%M UTC, %d %b')}</i>")
    return "\n".join(lines).strip()


def build_degraded_message(bad_handles, minutes):
    handles = "\n".join(f"• {h}" for h in bad_handles)
    return (
        "⚠️ <b>Monitor problem</b>\n\n"
        f"Couldn't read these product page(s) for ~{minutes} min:\n{handles}\n\n"
        "Stock checking for them is paused until this clears. If it persists, the product "
        "URL may have changed — reply and I'll fix it. (Other pages keep being watched.)"
    )


def build_heartbeat_message(avail_now, total, degraded_note):
    extra = f"\n{degraded_note}" if degraded_note else ""
    return (
        "✅ <b>one8 monitor — daily check-in</b>\n"
        "Watching Seam XVIII Signature (White + Red), all UK sizes.\n"
        f"Status: {avail_now}/{total} variants in stock right now."
        f"{extra}\n"
        "<i>You get this once a day. If it ever stops, the bot has stopped.</i>"
    )


# -------------------------------- main --------------------------------------

def run_once():
    state = load_state()
    prior_variants = state["variants"]

    seen, statuses = scan_products()
    expected = len(PRODUCT_HANDLES)
    readable = [h for h, s in statuses.items() if s == "ok"]
    bad_handles = [h for h, s in statuses.items() if s != "ok"]

    # ---- health / degraded tracking -------------------------------------
    if len(readable) == expected:
        state["fail_streak"] = 0
    else:
        state["fail_streak"] += 1

    if bad_handles and state["fail_streak"] >= DEGRADE_THRESHOLD \
            and state["fail_streak"] % DEGRADE_THRESHOLD == 0:
        minutes = state["fail_streak"] * 5
        log(f"DEGRADED: {bad_handles} unreadable for ~{minutes} min — warning")
        telegram_send(build_degraded_message(bad_handles, minutes))

    # ---- stock detection (only over variants we actually read) ----------
    rising_ids = [vid for vid, info in seen.items()
                  if info["available"] and not prior_variants.get(vid, False)]
    avail_now = sum(1 for i in seen.values() if i["available"])
    log(f"scanned {len(seen)} variants; readable colourways {len(readable)}/{expected}; "
        f"{avail_now} available; {len(rising_ids)} newly in stock; fail_streak={state['fail_streak']}")

    if rising_ids:
        log("rising edge(s) detected — re-fetching to confirm…")
        time.sleep(2)
        confirm, _ = scan_products()
        confirmed = [seen[vid] for vid in rising_ids if confirm.get(vid, {}).get("available")]
        if confirmed:
            for info in confirmed:
                log(f"CONFIRMED: {info['product_title']} / {info['colour']} / {info['size']}")
            telegram_send(build_restock_message(confirmed))
        else:
            log("confirmation failed/disagreed — treating as a blip, NO alert")

    # ---- update variant state (carry prior for unread handles) ----------
    for vid, info in seen.items():
        prior_variants[vid] = info["available"]
    state["variants"] = prior_variants

    # ---- daily heartbeat (also keeps the cron alive via its commit) -----
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if state["last_heartbeat_date"] != today and now.hour >= HEARTBEAT_UTC_HOUR:
        note = ("⚠️ Note: some pages currently unreadable." if bad_handles else "")
        if telegram_send(build_heartbeat_message(avail_now, len(seen) or expected, note)):
            state["last_heartbeat_date"] = today

    # ---- persist only on a meaningful change ----------------------------
    before = load_state()  # re-read to compare against on-disk signature
    if state_signature(state) != state_signature(before):
        write_state(state)
        log("state.json updated")
    else:
        log("no change — state.json left untouched")
    return 0


def run_test():
    log("sending Telegram test message…")
    ok = telegram_send(
        "✅ <b>one8 stock monitor</b> test.\n"
        "Watching: Seam XVIII Signature (White + Red), all UK sizes.\n"
        "Safeguards on: failure alerts, degraded-warnings, daily heartbeat.\n"
        "If you can read this, alerts are wired up correctly."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(run_test())
    sys.exit(run_once())
