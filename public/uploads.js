// Audio uploads (mic segments and whole-call parts): presigned PUT straight to storage, with an IndexedDB queue for retries
// (plan 8.2 step 3). Audio never goes through our functions.
import { api, sleep } from "/common.js";

const DB = "ti-uploads", STORE = "segments";

function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: "key" });
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function idbDo(mode, fn) {
  try {
    const db = await idb();
    return await new Promise((res, rej) => {
      const tx = db.transaction(STORE, mode);
      const out = fn(tx.objectStore(STORE));
      tx.oncomplete = () => res(out && out.result);
      tx.onerror = () => rej(tx.error);
    });
  } catch { return undefined; }   // private mode etc.: uploads still work, just no retry store
}

async function uploadOne(item) {
  const token = item.session_token;
  const signed = await api("/api/uploads/sign", { method: "POST", token, body: { seq: item.seq, kind: item.kind || "segment", content_type: item.blob.type || "audio/webm" } });
  const put = await fetch(signed.url, { method: signed.method || "PUT", headers: signed.headers || {}, body: item.blob });
  if (!put.ok) throw new Error(`upload failed (${put.status})`);
  await api("/api/uploads/complete", { method: "POST", token,
    body: { seq: item.seq, kind: item.kind || "segment", path: signed.path, start_ms: item.start_ms, end_ms: item.end_ms, bytes: item.blob.size } });
}

export class UploadQueue {
  constructor(session) { this.session = session; this.inflight = new Set(); this.failed = 0; }

  add({ kind = "segment", seq, start_ms, end_ms, blob }) {
    const key = `${this.session.session_id}:${kind === "segment" ? "" : kind + ":"}${seq}`;
    const item = { key, session_token: this.session.session_token, kind, seq, start_ms, end_ms, blob };
    const p = (async () => {
      for (let i = 0; i < 3; i++) {
        try { await uploadOne(item); return; } catch { await sleep(2000 * (i + 1)); }
      }
      this.failed++;
      await idbDo("readwrite", s => s.put(item));
    })().finally(() => this.inflight.delete(p));
    this.inflight.add(p);
  }

  pending() { return this.inflight.size + this.failed; }

  async drain(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (this.inflight.size && Date.now() < deadline) await sleep(250);
  }

  static async retryStored() {
    const items = await idbDo("readonly", s => s.getAll());
    for (const item of items || []) {
      try {
        await uploadOne(item);
        await idbDo("readwrite", s => s.delete(item.key));
      } catch (e) {
        if (e.status === 401) await idbDo("readwrite", s => s.delete(item.key));  // token expired: give up
      }
    }
  }
}
