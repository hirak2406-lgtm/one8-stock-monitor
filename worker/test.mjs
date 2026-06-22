// Local sanity test for the Worker's reply logic (no Telegram involved).
//   node worker/test.mjs "pavilion white"
import { buildReply } from "./worker.js";

const query = process.argv[2] || "seam xviii red";
const domain = process.env.STORE_DOMAIN || "one8.com";
console.log(await buildReply(domain, query));
