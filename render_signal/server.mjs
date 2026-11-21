import { createServer } from "node:http";
import worker, { Mailbox } from "./worker.mjs";

const CAP = 30000;
const MAX_ENTRIES = 1024;
const MAX_PENDING = 48;
const cells = new Map();
let pending = 0;

function expire() {
  const now = Date.now();
  for (const [key,value] of cells) {
    if (value.expires <= now) cells.delete(key);
  }
}
setInterval(expire, 15000).unref();

class CapacityError extends Error {}
const env = { MAILBOX: { getByName(room) {
  const prefix = room + "/";
  return new Mailbox({ storage: {
    async get(slot) { return cells.get(prefix + slot); },
    async put(slot,value) {
      const key = prefix + slot;
      if (!cells.has(key) && cells.size >= MAX_ENTRIES) {
        expire();
        if (cells.size >= MAX_ENTRIES) throw new CapacityError();
      }
      cells.set(key,value);
    },
    // The shared expiry timer performs the same cleanup on a single Node instance.
    async getAlarm() { return 0; },
    async setAlarm() {}
  }});
}}};

const server = createServer(async (req,res) => {
  const reject = (status) => {
    res.writeHead(status, {"Cache-Control":"no-store", "Connection":"close"});
    res.end();
  };
  if (pending >= MAX_PENDING) return reject(503);
  pending++;
  try {
    if (!req.url?.startsWith("/") || req.url.startsWith("//")) return reject(400);
    if (req.method !== "GET" && req.method !== "PUT") return reject(405);
    const chunks = [];
    let size = 0;
    const length = req.headers["content-length"];
    if (length !== undefined && (!/^\d+$/.test(length) || Number(length) > CAP)) return reject(413);
    let tooLarge = false;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > CAP) {
        tooLarge = true;
        break;
      }
      chunks.push(chunk);
    }
    if (tooLarge) {
      if (!req.destroyed) reject(413);
      return;
    }
    if (req.method === "GET" && size) return reject(400);
    const body = size ? Buffer.concat(chunks, size) : undefined;
    const result = await worker.fetch(new Request("http://localhost" + req.url,
      {method:req.method, headers:req.headers, body}),env);
    res.writeHead(result.status,Object.fromEntries(result.headers));
    res.end(Buffer.from(await result.arrayBuffer()));
  } catch (error) {
    if (!res.headersSent && !res.destroyed) reject(error instanceof CapacityError ? 503 : 500);
  } finally {
    pending--;
  }
});
server.requestTimeout = 15000;
server.headersTimeout = 10000;
server.setTimeout(15000, socket => socket.destroy());
const port = Number(process.env.PORT || 10000);
server.listen(port, process.env.BIND_HOST || "0.0.0.0", () => {
  console.log("LinkDesk Remote signaling listening on port " + server.address().port);
});

