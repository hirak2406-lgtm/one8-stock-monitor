#!/usr/bin/env python3
"""
iPhone 18 Pro Max — Apple Store pickup monitor (single store).

Watches ONE part at ONE Apple Store via Apple's `retail/pickup-message` API and fires a
Telegram + loud Pushover alert the moment it becomes available for in-store pickup.

Default target: iPhone 18 Pro Max, 256GB, Burgundy, unlocked (MJW64LL/A) at
Apple Christiana Mall, Newark DE (store R102, zip 19702).

Reliability model (same as the one8 monitor):
  - Authoritative signal = Apple's own `pickupDisplay` ("available"/"unavailable").
  - Edge-triggered off apple_state.json → one alert per transition, no spam.
  - Double-confirm (second fetch) before alerting → no blips.
  - Any error (non-200 / bad JSON / store missing) → keep prior state, NO alert.
  - Alert only marks state 'available' if at least one channel actually sent (never drop it).

Env overrides: APPLE_PART, APPLE_LOCATION (zip), APPLE_STORE (store number), APPLE_LABEL,
STATE_FILE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PUSHOVER_TOKEN, PUSHOVER_USER.
Flags: --test (send a test push and exit).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PART = os.environ.get("APPLE_PART", "MJW64LL/A")
LOCATION = os.environ.get("APPLE_LOCATION", "19702")
STORE_NUMBER = os.environ.get("APPLE_STORE", "R102")
PRODUCT_LABEL = os.environ.get("APPLE_LABEL", "iPhone 18 Pro Max 256GB Burgundy (unlocked)")
STORE_LABEL = os.environ.get("APPLE_STORE_LABEL", "Apple Christiana Mall — Newark, DE")
BUY_URL = os.environ.get(
    "APPLE_BUY_URL",
    "https://www.apple.com/shop/buy-iphone/iphone-18-pro/6.9-inch-display-256gb-burgundy-unlocked",
)
STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "apple_state.json")
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "").strip()
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "").strip()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")
HTTP_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF = 3


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ------------------------------ availability --------------------------------

def fetch_store_status():
    """
    Returns (available: bool, quote: str) for the target store, or (None, reason) on any
    error (caller must NOT alert on None).
    """
    url = (f"https://www.apple.com/shop/retail/pickup-message"
           f"?pl=true&parts.0={urllib.parse.quote(PART)}&location={LOCATION}&_={int(time.time()*1000)}")
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.apple.com/shop/buy-iphone/iphone-18-pro",
            })
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = json.loads(resp.read().decode("utf-8"))
            stores = data.get("body", {}).get("stores", [])
            store = next((s for s in stores if s.get("storeNumber") == STORE_NUMBER), None)
            if store is None:
                # Fall back to name match, else it's an error (don't alert).
                store = next((s for s in stores
                              if "christiana" in (s.get("storeName") or "").lower()), None)
            if store is None:
                return None, f"target store {STORE_NUMBER} not in response ({len(stores)} stores)"
            info = store.get("partsAvailability", {}).get(PART, {})
            disp = info.get("pickupDisplay")  # 'available' / 'unavailable'
            quote = info.get("pickupSearchQuote") or info.get("storeSelectionEnabled") or ""
            if disp not in ("available", "unavailable"):
                return None, f"unexpected pickupDisplay={disp!r}"
            return disp == "available", str(quote)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"fetch attempt {attempt}/{MAX_RETRIES} failed: {e!r}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    return None, f"giving up (last error: {last_err!r})"


# -------------------------------- state -------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"available": False, "last_checked": None}
    except Exception as e:  # noqa: BLE001
        log(f"state read failed ({e!r}); assuming prior=unavailable")
        return {"available": False, "last_checked": None}


def save_state(available):
    with open(STATE_FILE, "w") as f:
        json.dump({"available": bool(available),
                   "last_checked": datetime.now(timezone.utc).isoformat()}, f, indent=2)


# ----------------------------- notifications --------------------------------

def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram not configured — cannot send")
        return False
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode("utf-8")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(api, data=payload)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if json.loads(resp.read().decode("utf-8")).get("ok"):
                    log("Telegram sent")
                    return True
        except Exception as e:  # noqa: BLE001
            log(f"telegram attempt {attempt} failed: {e!r}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    log("Telegram FAILED")
    return False


def pushover_send(title, message, url="", url_title="Buy now"):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log("Pushover not configured — skipping loud alert")
        return False
    params = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title,
              "message": message, "priority": 2, "retry": 30, "expire": 3600,
              "sound": "persistent"}
    if url:
        params["url"] = url
        params["url_title"] = url_title
    data = urllib.parse.urlencode(params).encode("utf-8")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if json.loads(resp.read().decode("utf-8")).get("status") == 1:
                    log("Pushover EMERGENCY sent")
                    return True
        except Exception as e:  # noqa: BLE001
            log(f"pushover attempt {attempt} failed: {e!r}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    log("Pushover FAILED")
    return False


def build_telegram_message(quote):
    return (
        "🟢 <b>PICKUP AVAILABLE</b>\n\n"
        f"📱 <b>{PRODUCT_LABEL}</b>\n"
        f"🏬 {STORE_LABEL}\n"
        f"{('🕒 ' + quote) if quote else ''}\n\n"
        f'🔗 <a href="{BUY_URL}">Buy &amp; select pickup at Christiana</a>\n\n'
        f"<i>Detected {datetime.now(timezone.utc).strftime('%H:%M UTC, %d %b')}</i>"
    ).replace("\n\n\n", "\n\n")


# -------------------------------- main --------------------------------------

def run_once():
    available, detail = fetch_store_status()
    if available is None:
        log(f"no clean read ({detail}) — prior state kept, NO alert")
        return 0

    prev = load_state().get("available", False)
    log(f"{PRODUCT_LABEL} @ {STORE_NUMBER}: available={available} (prev={prev}) [{detail}]")

    if available and not prev:
        log("rising edge — re-fetching to confirm…")
        time.sleep(2)
        confirm, _ = fetch_store_status()
        if confirm is True:
            tg_ok = telegram_send(build_telegram_message(detail))
            po_ok = pushover_send(
                "🟢 iPhone 18 Pro Max — Christiana",
                f"{PRODUCT_LABEL} is available for pickup at {STORE_LABEL}. Tap to buy.",
                BUY_URL, "Buy now")
            if tg_ok or po_ok:
                save_state(True)
            else:
                log("alert FAILED on both channels — state NOT advanced; retry next cycle")
        else:
            log("confirmation disagreed — blip, NO alert")
        return 0

    if (not available) and prev:
        log("went unavailable again")
        save_state(False)
        return 0

    # No change — leave state untouched (avoids commit churn).
    log("no change")
    return 0


def run_test():
    ok = pushover_send(
        "🧪 iPhone 18 monitor — TEST",
        f"Watching {PRODUCT_LABEL} at {STORE_LABEL}. If you got this loud, alerts work.",
        BUY_URL, "Open")
    telegram_send(f"🧪 <b>iPhone 18 monitor test</b>\nWatching {PRODUCT_LABEL} at {STORE_LABEL}.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_test() if "--test" in sys.argv else run_once())
