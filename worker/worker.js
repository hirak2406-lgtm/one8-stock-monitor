/**
 * one8.com conversational stock checker — Cloudflare Worker (webhook mode).
 *
 * Telegram pushes each message here via setWebhook, so replies are near-instant (~1-3s),
 * fully cloud-hosted, and work with your Mac off. One-off checks only — this does NOT
 * change what the 24/7 stock monitor watches.
 *
 * Port of the proven logic in ../bot_poll.py. Only messages from TELEGRAM_CHAT_ID are
 * answered, and requests must carry the webhook secret header.
 *
 * Secrets (set via `wrangler secret put`, never committed):
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET, STORE_DOMAIN (optional)
 */

const DEFAULT_STORE = "one8.com";
const MAX_MATCHES = 4;
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const STOPWORDS = new Set(
  ["the", "a", "in", "stock", "shoe", "shoes", "size", "is", "available", "for", "of"]
);

function esc(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function httpJson(url) {
  const r = await fetch(url, {
    headers: {
      "User-Agent": UA,
      "Accept": "application/json",
      // Pin Shopify Markets to India so prices are always the base INR the user pays,
      // not a geo-localized currency (the Worker runs on edges worldwide).
      "Cookie": "localization=IN; cart_currency=INR",
    },
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return await r.json();
}

async function fetchCatalog(domain) {
  let products = [];
  for (let page = 1; page <= 6; page++) {
    let d;
    try {
      d = await httpJson(`https://${domain}/products.json?limit=250&page=${page}`);
    } catch (e) {
      break;
    }
    const items = d.products || [];
    if (!items.length) break;
    products = products.concat(items);
    if (items.length < 250) break; // last page reached
  }
  return products;
}

async function fetchProductJs(domain, handle) {
  try {
    return await httpJson(`https://${domain}/products/${handle}.js?_=${Date.now()}`);
  } catch (e) {
    return null;
  }
}

function searchCatalog(query, products) {
  const toks = query.toLowerCase().split(/\W+/).filter((t) => t && !STOPWORDS.has(t));
  if (!toks.length) return [];
  const scored = [];
  for (const p of products) {
    const hay = ((p.title || "") + " " + (p.handle || "")).toLowerCase();
    let score = 0;
    for (const t of toks) if (hay.includes(t)) score++;
    if (score) scored.push([score, p]);
  }
  if (!scored.length) return [];
  scored.sort((a, b) => b[0] - a[0]);
  const best = scored[0][0];
  return scored.filter((x) => x[0] === best).slice(0, MAX_MATCHES).map((x) => x[1]);
}

function sizeKey(s) {
  const m = String(s || "").match(/\d+/);
  return m ? parseInt(m[0], 10) : 999;
}

function priceStr(priceMinor) {
  if (priceMinor == null) return "price n/a";
  const n = Math.round(priceMinor / 100);
  return "₹" + n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

async function productStatusBlock(domain, handle) {
  const data = await fetchProductJs(domain, handle);
  if (!data || !data.variants) return null;
  const title = data.title || handle;
  let colour = "";
  let price = null;
  const inStock = [];
  const soldOut = [];
  for (const v of data.variants) {
    colour = v.option1 || colour;
    if (price === null) price = v.price;
    const size = v.option2 || v.title || "";
    (v.available ? inStock : soldOut).push(size);
  }
  inStock.sort((a, b) => sizeKey(a) - sizeKey(b));
  const url = `https://${domain}/products/${handle}`;
  const lines = [`👟 <b>${esc(title)}</b>`];
  if (colour) lines.push(`Colour: ${esc(colour)}`);
  if (inStock.length) lines.push(`✅ In stock: <b>${esc(inStock.join(", "))}</b>`);
  else lines.push("❌ Sold out (all sizes)");
  lines.push(`Price: ${priceStr(price)}`);
  lines.push(`🔗 <a href="${url}">View product</a>`);
  return lines.join("\n");
}

export async function buildReply(domain, query) {
  const products = await fetchCatalog(domain);
  if (!products.length) return "⚠️ Couldn't reach one8.com just now — try again in a minute.";
  const matches = searchCatalog(query, products);
  if (!matches.length) {
    return `🔎 No one8.com product matched “${esc(query)}”.\n` +
           "Try fewer / different words, e.g. “seam xviii red” or “pavilion white”.";
  }
  const blocks = [];
  for (const p of matches) {
    const b = await productStatusBlock(domain, p.handle);
    if (b) blocks.push(b);
  }
  if (!blocks.length) {
    return "⚠️ Found the product but couldn't read its stock just now — try again shortly.";
  }
  const header = `🔎 Results for “${esc(query)}”` +
                 (blocks.length > 1 ? ` — ${blocks.length} matches:` : ":");
  return header + "\n\n" + blocks.join("\n\n");
}

const HELP =
  "👋 Send me any one8.com product name and I'll check its stock.\n" +
  "Examples: “seam xviii red”, “pavilion white”, “sonic curve leggings”.\n" +
  "(One-off check — your 24/7 shoe monitor keeps running separately.)";

async function sendMessage(env, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: env.TELEGRAM_CHAT_ID,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    }),
  });
}

export default {
  async fetch(request, env, ctx) {
    // Health check / browser hit.
    if (request.method !== "POST") return new Response("one8 chat bot: ok");

    // Only Telegram (carrying our secret header) may trigger work.
    if (env.WEBHOOK_SECRET &&
        request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch (e) {
      return new Response("ok"); // malformed — ack so Telegram doesn't retry forever
    }

    const msg = update.message || update.edited_message;
    const chatId = String((msg && msg.chat && msg.chat.id) || "");
    const text = ((msg && msg.text) || "").trim();

    // Ignore anything that isn't a text message from the owner.
    if (!msg || !text || chatId !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response("ok");
    }

    const domain = env.STORE_DOMAIN || DEFAULT_STORE;
    const work = (async () => {
      try {
        const lower = text.toLowerCase();
        if (lower === "/start" || lower === "/help" || lower === "help") {
          await sendMessage(env, HELP);
          return;
        }
        const query = text.replace(/^\/check\s+/i, "").trim();
        await sendMessage(env, await buildReply(domain, query));
      } catch (e) {
        try {
          await sendMessage(env, "⚠️ Couldn't check that just now — please try again.");
        } catch (_) { /* swallow */ }
      }
    })();

    // Return 200 immediately; finish the reply in the background.
    ctx.waitUntil(work);
    return new Response("ok");
  },
};
