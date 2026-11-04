// Text chat (plan 8.4): bursts are grouped into one subject turn after a few idle seconds,
// the fast lane returns the reply, the slow lane (/analyze) runs in the background.
import { api, apiRetry, inviteToken, uid } from "/common.js";

const $ = id => document.getElementById(id);
const log = $("log"), input = $("input");
let session = null;
let burst = [];            // [{text, at_ms}]
let promptAt = performance.now();
let timer = null;
let busy = false;

function bubble(speaker, text, extra = "") {
  const div = document.createElement("div");
  div.className = `msg ${speaker} ${extra}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function showError(msg) {
  $("error").textContent = msg || "";
  $("error").hidden = !msg;
}

async function start() {
  try {
    session = await api("/api/sessions", { method: "POST", body: { invite_token: inviteToken(), modality: "text" } });
  } catch (e) {
    $("status").textContent = "";
    showError(e.message);
    $("form").hidden = true;
    return;
  }
  for (const t of session.history) bubble(t.speaker, t.text);
  $("status").textContent = session.resumed ? "welcome back" : "";
  promptAt = performance.now();
  input.focus();
}

function scheduleFlush() {
  clearTimeout(timer);
  timer = setTimeout(flush, (session?.burst_idle_s || 8) * 1000);
}

async function flush() {
  if (!burst.length || busy) return;
  busy = true;
  const messages = burst;
  burst = [];
  const body = {
    client_turn_id: uid(),
    subject: { text: messages.map(m => m.text).join("\n") },
    messages,
  };
  const typing = bubble("interviewer", "…", "typing");
  try {
    const res = await apiRetry("/api/turn", { method: "POST", body, token: session.session_token });
    typing.remove();
    if (res.reply) bubble("interviewer", res.reply);
    promptAt = performance.now();
    showError("");
    if (res.wrap_up) $("end").classList.add("primary");
    // Slow lane: don't block the chat on it.
    apiRetry(`/api/turn/${res.turn_id}/analyze`, { method: "POST", token: session.session_token })
      .catch(() => { /* the backlog job will catch up */ });
  } catch (e) {
    typing.remove();
    burst = messages.concat(burst);   // keep text; same messages retried on next send
    showError(e.message);
  } finally {
    busy = false;
    if (burst.length) scheduleFlush();
  }
}

$("form").addEventListener("submit", ev => {
  ev.preventDefault();
  const text = input.value.trim();
  if (!text || !session) return;
  input.value = "";
  bubble("subject", text);
  burst.push({ text, at_ms: Math.round(performance.now() - promptAt) });
  scheduleFlush();
});

input.addEventListener("keydown", ev => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("form").requestSubmit(); }
});
// Typing extends the burst window, so a person mid-thought isn't cut off.
input.addEventListener("input", () => { if (burst.length) scheduleFlush(); });

$("end").addEventListener("click", async () => {
  clearTimeout(timer);
  await flush();
  try {
    await api(`/api/sessions/${session.session_id}/end`, { method: "POST", body: { reason: "user" }, token: session.session_token });
  } catch { /* already ended */ }
  bubble("interviewer", "Thanks! That's saved. You can come back to this link any time.");
  $("form").hidden = true;
  $("end").disabled = true;
});

start();
