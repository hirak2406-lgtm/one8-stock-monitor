#!/usr/bin/env python3
"""
Stock monitor for one8.com — Shopify variant availability watcher.

Watches a single Shopify product variant (default: "Seam XVIII Signature - White",
UK 8) and sends a Telegram push the moment it transitions from sold-out to in-stock.

Design goals: NEVER miss a restock, NEVER send a false alert.
  - Source of truth = Shopify's own `available` flag from the product `.js` endpoint.
  - Match by variant ID (stable across handle/URL/ordering changes).
  - Any error (network / non-200 / bad JSON / missing variant) => log + exit, no alert.
  - Cache-buster + double-confirm second fetch => no stale-CDN false positives.
  - Edge-trigger off a durable state.json => one alert per restock, lose-state re-alerts.

Configuration (env vars override defaults):
  PRODUCT_HANDLE       Shopify product handle (default: seam-xviii-signature-mens-white)
  VARIANT_ID           Variant ID to watch    (default: 57738053648544  -> UK 8)
  STORE_DOMAIN         Store domain           (default: one8.com)
  STATE_FILE           Path to state file     (default: ./state.json)
  TELEGRAM_BOT_TOKEN   Telegram bot token     (required to actually send)
  TELEGRAM_CHAT_ID     Telegram chat id       (required to actually send)

Flags:
  --once    Run a single check (default; the cron calls it this way).
  --test    Send a test Telegram message and exit (verifies your credentials).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------- configuration --------------------------------

STORE_DOMAIN = os.environ.get("STORE_DOMAIN", "one8.com")
PRODUCT_HANDLE = os.environ.get("PRODUCT_HANDLE", "seam-xviii-signature-mens-white")
VARIANT_ID = int(os.environ.get("VARIANT_ID", "57738053648544"))  # UK 8
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HTTP_TIMEOUT = 15        # seconds per request
MAX_RETRIES = 3          # network retries before giving up this cycle
RETRY_BACKOFF = 3        # seconds, multiplied by attempt number


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ------------------------------ networking ----------------------------------

def product_js_url():
    # Cache-buster defeats Shopify CDN staleness so we always read fresh stock.
    cache_buster = int(time.time() * 1000)
    return (
        f"https://{STORE_DOMAIN}/products/{PRODUCT_HANDLE}.js"
        f"?_={cache_buster}"
    )


def fetch_product():
    """Fetch + parse the product JSON. Returns dict, or None on any failure."""
    url = product_js_url()
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
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001 - any failure must be non-fatal
            last_err = e
            log(f"fetch attempt {attempt}/{MAX_RETRIES} failed: {e!r}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    log(f"giving up this cycle (last error: {last_err!r}) — NO alert sent")
    return None


def find_variant(product):
    """Return the watched variant dict, or None if not present."""
    if not product or "variants" not in product:
        return None
    for v in product["variants"]:
        if v.get("id") == VARIANT_ID:
            return v
    return None


def check_available():
    """
    Returns (available: bool, variant: dict, product: dict) on a clean read,
    or (None, None, None) if anything went wrong (=> caller must NOT alert).
    """
    product = fetch_product()
    if product is None:
        return None, None, None
    variant = find_variant(product)
    if variant is None:
        log(f"variant {VARIANT_ID} not found in product JSON — NO alert sent")
        return None, None, None
    return bool(variant.get("available")), variant, product


# -------------------------------- state -------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # No prior state. Default available=False so a *current* in-stock read
        # still fires an alert (errs toward notifying, never toward silence).
        return {"available": False, "last_checked": None}
    except Exception as e:  # noqa: BLE001
        log(f"state read failed ({e!r}); treating prior state as unknown/False")
        return {"available": False, "last_checked": None}


def save_state(available):
    state = {
        "available": bool(available),
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log(f"state saved: available={available}")


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
        "disable_web_page_preview": "false",
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


def price_str(variant):
    # Shopify .js prices are in minor units (paise). Format as rupees.
    try:
        return f"₹{variant['price'] / 100:,.0f}"
    except Exception:  # noqa: BLE001
        return "price n/a"


def restock_message(variant, product):
    product_url = f"https://{STORE_DOMAIN}/products/{PRODUCT_HANDLE}?variant={VARIANT_ID}"
    cart_url = f"https://{STORE_DOMAIN}/cart/{VARIANT_ID}:1"
    size = variant.get("title", "").split("/")[-1].strip() or "selected size"
    name = product.get("title", PRODUCT_HANDLE)
    return (
        f"🟢 <b>BACK IN STOCK</b>\n\n"
        f"<b>{name}</b>\n"
        f"Size: <b>{size}</b>\n"
        f"Price: {price_str(variant)}\n\n"
        f"🛒 <a href=\"{cart_url}\">Add to cart now</a>\n"
        f"🔗 <a href=\"{product_url}\">Product page</a>\n\n"
        f"<i>Detected {datetime.now(timezone.utc).strftime('%H:%M:%SZ')} — go fast.</i>"
    )


# -------------------------------- main --------------------------------------

def run_once():
    available, variant, product = check_available()

    if available is None:
        # Error already logged. Do not touch state, do not alert.
        return 0

    prev = load_state().get("available", False)
    log(f"variant {VARIANT_ID}: available={available} (previous={prev})")

    if available and not prev:
        # Rising edge. DOUBLE-CONFIRM before alerting to kill CDN/cache blips.
        log("rising edge detected — re-fetching to confirm…")
        time.sleep(2)
        confirm, c_variant, c_product = check_available()
        if confirm is True:
            log("confirmed in stock — sending alert")
            telegram_send(restock_message(c_variant or variant, c_product or product))
            save_state(True)
        else:
            log("confirmation failed/disagreed — treating as a blip, NO alert")
            # Do not persist True; next cycle re-evaluates cleanly.
        return 0

    if (not available) and prev:
        log("went out of stock again")
        save_state(False)
        return 0

    # No availability change. Deliberately do NOT rewrite state.json so the cron
    # only commits on real transitions (no every-5-min commit churn).
    log("no change — state.json left untouched")
    return 0


def run_test():
    log("sending Telegram test message…")
    ok = telegram_send(
        "✅ <b>one8 stock monitor</b> test message.\n"
        "If you can read this, alerts are wired up correctly."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(run_test())
    sys.exit(run_once())
