#!/usr/bin/env python3
"""
Stock monitor for one8.com — Shopify availability watcher.

Watches the **Seam XVIII Signature (men's)** sneaker across BOTH colourways and ALL
sizes, and sends a clean Telegram push the moment any variant comes back in stock.

Design goals: NEVER miss a restock, NEVER send a false alert.
  - Source of truth = Shopify's own `available` flag from each product `.js` endpoint.
  - Track every variant by ID (stable across handle/URL/ordering changes).
  - Any error (network / non-200 / bad JSON / missing product) => carry prior state,
    no alert. An error must never look like "in stock" or "out of stock".
  - Cache-buster + a confirming second fetch => no stale-CDN false positives.
  - Edge-triggered off a durable state.json => one alert per restock, lose-state re-alerts.

Configuration (env vars override defaults):
  PRODUCT_HANDLES   Comma-separated Shopify handles to watch
                    (default: the white + red Signature colourways)
  STORE_DOMAIN      Store domain (default: one8.com)
  STATE_FILE        Path to state file (default: ./state.json)
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   Telegram credentials (required to send)

Flags:
  --test    Send a test Telegram message and exit (verifies your credentials).
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------- configuration --------------------------------

STORE_DOMAIN = os.environ.get("STORE_DOMAIN", "one8.com")

# Both colourways of the men's Seam XVIII Signature. All sizes within each are watched.
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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HTTP_TIMEOUT = 15        # seconds per request
MAX_RETRIES = 3          # network retries before giving up on a product this cycle
RETRY_BACKOFF = 3        # seconds, multiplied by attempt number


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ------------------------------ networking ----------------------------------

def fetch_product(handle):
    """Fetch + parse one product's JSON. Returns dict, or None on any failure."""
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
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - any failure must be non-fatal
            last_err = e
            log(f"[{handle}] fetch attempt {attempt}/{MAX_RETRIES} failed: {e!r}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    log(f"[{handle}] giving up this cycle (last error: {last_err!r}) — prior state kept")
    return None


def scan_products():
    """
    Fetch all watched products. Returns dict:
      { variant_id: {"available": bool, "handle", "product_title",
                     "colour", "size", "price"} }
    Products that fail to fetch are simply absent (caller carries prior state).
    """
    seen = {}
    for handle in PRODUCT_HANDLES:
        product = fetch_product(handle)
        if not product or "variants" not in product:
            continue
        for v in product["variants"]:
            vid = v.get("id")
            if vid is None:
                continue
            seen[vid] = {
                "available": bool(v.get("available")),
                "handle": handle,
                "product_title": product.get("title", handle),
                "colour": v.get("option1") or "",
                "size": v.get("option2") or v.get("title", ""),
                "price": v.get("price"),
            }
    return seen


# -------------------------------- state -------------------------------------

def load_prior():
    """Return {variant_id(int): available(bool)} from the state file (empty if none)."""
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        log(f"state read failed ({e!r}); treating all prior states as unknown/False")
        return {}
    variants = data.get("variants", {})
    out = {}
    for k, val in variants.items():
        try:
            out[int(k)] = bool(val)
        except (ValueError, TypeError):
            continue
    return out


def write_state(state_map):
    payload = {
        "variants": {str(k): bool(v) for k, v in sorted(state_map.items())},
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


def build_message(restocked):
    """
    restocked: list of variant-info dicts (the `seen` values) that are newly available.
    Produces ONE clean message, grouped by product/colour. Product link only (no cart).
    """
    # Group by handle.
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


# -------------------------------- main --------------------------------------

def run_once():
    prior = load_prior()
    seen = scan_products()

    if not seen:
        log("no products could be read this cycle — prior state kept, NO alert")
        return 0

    # Detect rising edges: variant went (prior False / unknown) -> now True.
    rising_ids = [vid for vid, info in seen.items()
                  if info["available"] and not prior.get(vid, False)]

    total = len(seen)
    avail_now = sum(1 for i in seen.values() if i["available"])
    log(f"scanned {total} variants across {len(PRODUCT_HANDLES)} colourways "
        f"— {avail_now} available, {len(rising_ids)} newly in stock")

    if rising_ids:
        log("rising edge(s) detected — re-fetching to confirm…")
        time.sleep(2)
        confirm = scan_products()
        confirmed = [seen[vid] for vid in rising_ids
                     if confirm.get(vid, {}).get("available")]
        if confirmed:
            for info in confirmed:
                log(f"CONFIRMED in stock: {info['product_title']} / {info['colour']} / {info['size']}")
            telegram_send(build_message(confirmed))
        else:
            log("confirmation failed/disagreed — treating as a blip, NO alert")

    # Build the new state. Start from prior (so products that failed to load keep their
    # last-known value — never falsely flipped to out-of-stock), then apply what we saw.
    new_state = dict(prior)
    for vid, info in seen.items():
        new_state[vid] = info["available"]

    # Persist only when something actually changed (avoids 5-min commit churn).
    if new_state != prior:
        write_state(new_state)
        log("state.json updated")
    else:
        log("no change — state.json left untouched")
    return 0


def run_test():
    log("sending Telegram test message…")
    ok = telegram_send(
        "✅ <b>one8 stock monitor</b> test.\n"
        "Watching: Seam XVIII Signature (White + Red), all UK sizes.\n"
        "If you can read this, alerts are wired up correctly."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(run_test())
    sys.exit(run_once())
