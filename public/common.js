// Shared helpers for the subject and creator pages.
export async function api(path, { method = "GET", body, token, headers = {} } = {}) {
  const h = { ...headers };
  if (body !== undefined) h["content-type"] = "application/json";
  if (token) h["authorization"] = `Bearer ${token}`;
  const res = await fetch(path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.detail && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export function inviteToken() {
  const m = location.pathname.match(/^\/s\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

export function uid() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    "xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx".replace(/[xy]/g, c => ((Math.random() * 16) | 0).toString(16));
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));

// Retries 409 "busy" responses with the same body (same client_turn_id → idempotent).
export async function apiRetry(path, opts, tries = 5) {
  for (let i = 0; ; i++) {
    try { return await api(path, opts); }
    catch (e) {
      if (i >= tries - 1 || !(e.status === 409 && /Still working/.test(e.message)) && !(e.status >= 500 || !e.status)) throw e;
      await sleep(800 * (i + 1));
    }
  }
}
