# one8.com Stock Monitor 👟

Watches a single Shopify variant on **one8.com** and sends a **Telegram** push the moment
it comes back in stock. Runs free in the cloud via **GitHub Actions** (every 5 min), so it
works even when your Mac is off.

**Default target:** `Seam XVIII Signature - White`, **UK 8** (variant `57738053648544`).

## Why it's reliable (no missed / no false alerts)

- Reads Shopify's own `available` flag from `…/products/<handle>.js` — the same flag the
  store's "Add to Cart" button obeys. No HTML scraping.
- Matches by **variant ID**, so it survives URL / ordering changes.
- Any error (network, non-200, bad JSON, missing variant) → logs and exits, **never** alerts.
- Cache-buster + a **second confirming fetch** before alerting → no stale-CDN false positives.
- **Edge-triggered** off `state.json` → exactly one alert per restock; if state is ever lost
  it re-alerts rather than going silent.

No third-party Python packages — standard library only.

---

## Setup

### 1. Create a Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the **bot token**.
2. Open your new bot and send it any message (e.g. `hi`).
3. Get your chat ID:
   ```bash
   TELEGRAM_BOT_TOKEN=<your-token> python3 get_chat_id.py
   ```
   Copy the printed `TELEGRAM_CHAT_ID`.

### 2. Test locally
```bash
export TELEGRAM_BOT_TOKEN=<your-token>
export TELEGRAM_CHAT_ID=<your-chat-id>

python3 monitor.py --test     # sends a test push — check your phone
python3 monitor.py            # one real check (will say "not available" right now)
```

### 3. Deploy to GitHub Actions (the cloud part)
1. Create a **private** GitHub repo and push this folder to it.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. **Actions** tab → enable workflows → run **one8 stock monitor** once via **Run workflow**
   to confirm it goes green. After that the cron runs every 5 minutes automatically.

---

## Changing the watched size / product

Edit the env defaults in [`.github/workflows/stock-monitor.yml`](.github/workflows/stock-monitor.yml)
(or set `VARIANT_ID` / `PRODUCT_HANDLE` locally). To find a variant ID, open
`https://one8.com/products/<handle>.js` and read the `variants[].id` for the size you want.

## Notes / trade-offs

- GitHub Actions cron is **~5-min** granularity and can lag under load. Fine for a normal
  "Coming Soon → restock". For sub-minute polling you'd need an always-on machine
  (e.g. a `launchd` agent) — ask and we can add one.
- `state.json` is committed by the bot **only when availability flips**, so the repo isn't
  spammed with commits.
