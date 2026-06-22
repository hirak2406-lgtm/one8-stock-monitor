# one8.com Stock Monitor + Chat Checker 👟

Two cooperating Telegram bots for **one8.com** (a Shopify store):

1. **24/7 stock monitor** — watches the **Seam XVIII Signature** sneaker across **both
   colourways** (White = `seam-xviii-signature-mens-white`, Red = `seam-xviii-signature-mens-red`)
   and **all UK sizes**, and pushes a Telegram alert the moment any of them restocks.
   Runs on **GitHub Actions** (cloud, works with your Mac off).
2. **Conversational checker** — message the bot any one8.com product name and it replies
   with that product's current stock. Runs as a **Cloudflare Worker webhook** (~1–3 s replies).

## Why the monitor is reliable (no missed / no false alerts)

- Reads Shopify's own `available` flag from `…/products/<handle>.js` — the authoritative
  signal, not HTML scraping. Matches by **variant ID**, so it survives URL/ordering changes.
- Any fetch error (network / non-200 / bad JSON) → keeps prior state, **never** alerts.
- Cache-buster + a **second confirming fetch** before alerting → no stale-CDN false positives.
- **Edge-triggered** off `state.json` → one alert per restock. A restock alert is **not**
  marked done unless the Telegram send actually succeeds (so a send failure re-alerts, never drops).
- Prices pinned to base **INR** via a `localization=IN` cookie (Shopify Markets otherwise
  geo-localizes the currency on non-India servers).

## Self-monitoring ("tell me if it breaks")

- **Failure alert:** a workflow step pings you if any monitor run crashes.
- **Degraded warning:** if a product page is unreadable for a sustained period, you're warned.
- **Daily heartbeat:** a once-a-day "✅ still watching" message; its commit also keeps the
  GitHub cron from idle-disabling. If the heartbeat stops, the bot has stopped.
- **Chat-bot watchdog:** each monitor run checks the chat bot's webhook health and alerts you
  (and on recovery) if it stops answering.

No third-party Python packages — standard library only.

## Files

| File | Role |
|---|---|
| `monitor.py` | 24/7 stock monitor + self-monitoring + chat-bot watchdog |
| `state.json` | Durable state (per-variant availability, fail streak, heartbeat date, webhook flag) |
| `.github/workflows/stock-monitor.yml` | Cron (every 30 min) + manual run; commits state; failure alert |
| `worker/worker.js` | Cloudflare Worker — the conversational checker (webhook) |
| `worker/wrangler.toml`, `worker/package.json` | Worker deploy config |
| `worker/test.mjs` | Local test: `node worker/test.mjs "pavilion white"` |
| `get_chat_id.py` | One-time helper to find your Telegram chat ID |
| `bot_poll.py` | *Legacy* polling version of the chat checker (superseded by the Worker) |

## Configuration

Monitor env (defaults in `stock-monitor.yml`):
- `PRODUCT_HANDLES` — comma-separated handles to watch (default: the two Signature colourways)
- `POLL_INTERVAL_MIN` — used for the degraded-warning text; keep in sync with the cron (30)
- `HEARTBEAT_UTC_HOUR` — hour (UTC) for the daily heartbeat (default 4 ≈ 9:30 IST)
- `CHAT_WEBHOOK_URL` — the chat Worker URL the watchdog checks

Secrets are **never committed**: `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` live in GitHub
Actions secrets (monitor) and Cloudflare Worker secrets (chat bot, plus `WEBHOOK_SECRET`).

## Cost (free tier)

- **GitHub Actions:** private repo = 2,000 min/month. At every 30 min (~1,440 min/month) it
  stays free. **Every 5 min would exceed the cap** — to run that fast for free, make the repo
  **public** (Actions is then unlimited; no secrets are in the code).
- **Cloudflare Worker + Telegram:** free, tiny usage.

## Common tasks

- **Watch a different product/size:** set `PRODUCT_HANDLES` in `stock-monitor.yml`. Find a
  handle at `https://one8.com/products.json`.
- **Redeploy the chat Worker:** `cd worker && CLOUDFLARE_API_TOKEN=… CLOUDFLARE_ACCOUNT_ID=… npx wrangler@3 deploy`
- **Revert chat bot to polling:** `curl .../deleteWebhook` and re-enable a poller.
- **Test the monitor locally:** `TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=… python3 monitor.py --test`
