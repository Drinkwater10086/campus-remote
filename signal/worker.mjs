const CAP = 30000;
const LIFE = 120000;
const match = /^\/v1\/([a-f0-9]{64})\/(request|(?:offer|answer)-[a-f0-9]{24})$/;
const headers = { "Cache-Control": "no-store" };

export default {
  fetch(request, env) {
    const pathname = new URL(request.url).pathname;
    if (pathname === "/health" && request.method === "GET") {
      return new Response("campus-remote-signal-v1", { headers });
    }
    const path = match.exec(pathname);
    if (!path) return new Response(null, { status: 404, headers });
    return env.MAILBOX.getByName(path[1]).fetch(request);
  }
};

export class Mailbox {
  constructor(ctx) {
    this.storage = ctx.storage;
  }

  async fetch(request) {
    const path = match.exec(new URL(request.url).pathname);
    if (!path) return new Response(null,{status:404,headers});
    const slot = path[2];
    if (request.method === "GET") {
      const entry = await this.storage.get(slot);
      if (!entry || entry.expires <= Date.now()) return new Response(null,{status:404,headers});
      return new Response(entry.body, { headers: {...headers, "Content-Type":"application/octet-stream"} });
    }
    if (request.method !== "PUT") return new Response(null,{status:405,headers});
    const length = request.headers.get("Content-Length");
    const size = length === null ? null : Number(length);
    if (size !== null && (!Number.isInteger(size) || size < 32 || size > CAP)) return new Response(null,{status:413,headers});
    const reader = request.body?.getReader();
    if (!reader) return new Response(null,{status:400,headers});
    const bytes = new Uint8Array(CAP);
    let offset = 0;
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      if (offset + value.length > CAP) {
        await reader.cancel();
        return new Response(null,{status:413,headers});
      }
      bytes.set(value,offset);
      offset += value.length;
    }
    const body = new TextDecoder().decode(bytes.subarray(0,offset));
    if (body.length < 32 || (size !== null && body.length !== size) || !/^[A-Za-z0-9_=-]+$/.test(body)) return new Response(null,{status:400,headers});
    const expires = Date.now() + LIFE;
    await this.storage.put(slot,{body,expires});
    if ((await this.storage.getAlarm()) === null) await this.storage.setAlarm(expires);
    return new Response(null,{status:204,headers});
  }

  async alarm() {
    const now = Date.now();
    let next = Infinity;
    for (const [key,value] of await this.storage.list()) {
      if (value.expires <= now) await this.storage.delete(key);
      else next = Math.min(next,value.expires);
    }
    if (Number.isFinite(next)) await this.storage.setAlarm(next);
  }
}



