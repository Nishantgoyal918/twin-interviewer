// Creator console: sign in, describe a twin, review/approve the spec, invite the subject,
// watch coverage, inspect every turn (debug view) and label turns for signal validation.
import { api, esc, uid } from "/common.js";

const $ = id => document.getElementById(id);
const view = $("view");
let cfg, supa = null, authHeaders = {};

function err(msg) { $("error").textContent = msg || ""; $("error").hidden = !msg; }
const call = (path, opts = {}) => api(path, { ...opts, headers: { ...authHeaders, ...(opts.headers || {}) } });

async function auth() {
  cfg = await api("/api/public-config");
  if (cfg.dev_auth) {
    // ?as=<id> views another dev creator (the simulator's twins) without changing your own.
    const as = new URLSearchParams(location.search).get("as");
    let id = as || localStorage.getItem("ti:dev-creator");
    if (!id) { id = uid(); localStorage.setItem("ti:dev-creator", id); }
    authHeaders = { "x-dev-creator": id };
    $("who").textContent = as ? "dev mode · simulator" : "dev mode";
    return true;
  }
  if (cfg.open_access) {
    // Public demo: no sign-in; everyone shares the same twins.
    $("who").textContent = "open demo · shared with everyone";
    return true;
  }
  supa = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key);
  const { data } = await supa.auth.getSession();
  if (!data.session) return false;
  authHeaders = { authorization: `Bearer ${data.session.access_token}` };
  $("who").innerHTML = `${esc(data.session.user.email)} · <a href="#" id="signout">sign out</a>`;
  $("signout").onclick = async e => { e.preventDefault(); await supa.auth.signOut(); location.reload(); };
  supa.auth.onAuthStateChange((_e, s) => { if (s) authHeaders = { authorization: `Bearer ${s.access_token}` }; });
  return true;
}

function signInView() {
  view.innerHTML = `
    <div class="card stack" style="max-width:420px">
      <h2>Sign in</h2>
      <p class="muted">We'll email you a sign-in link.</p>
      <label for="email">Email</label><input id="email" type="email" autocomplete="email">
      <button class="primary" id="send">Send link</button>
      <p id="sent" class="muted" hidden>Check your inbox.</p>
    </div>`;
  $("send").onclick = async () => {
    const { error } = await supa.auth.signInWithOtp({ email: $("email").value, options: { emailRedirectTo: location.origin + "/creator" } });
    if (error) err(error.message); else $("sent").hidden = false;
  };
}

// ---------------- home: list + new twin ----------------
async function home() {
  const twins = await call("/api/twins");
  view.innerHTML = `
    <section class="card stack">
      <h2>New twin</h2>
      <label for="brief" class="muted">Describe who and why, in your own words.</label>
      <textarea id="brief" rows="4" placeholder="A twin of my mother so I can ask her recipes at night. She speaks Hindi and English…"></textarea>
      <div><button class="primary" id="create">Create</button></div>
    </section>
    <section class="stack">
      <h2>Your twins</h2>
      ${twins.length ? `<table><thead><tr><th>Subject</th><th>Status</th><th>Spec</th><th>Sessions</th></tr></thead><tbody>
        ${twins.map(t => `<tr><td><a href="#/twin/${t.id}">${esc(t.subject_name)}</a>${t.simulated ? ` <span class="pill sim" title="A simulated person (persona agent) took these interviews">simulated</span>` : ""}</td><td><span class="pill">${esc(t.status)}</span></td>
          <td>v${t.latest_version ?? "–"}</td><td>${t.sessions}</td></tr>`).join("")}</tbody></table>`
        : `<p class="muted">None yet. Describe your first one above.</p>`}
    </section>`;
  $("create").onclick = async () => {
    $("create").disabled = true; $("create").textContent = "Thinking…";
    try { const r = await call("/api/twins", { method: "POST", body: { brief: $("brief").value } }); location.hash = `#/twin/${r.twin_id}`; }
    catch (e) { err(e.message); $("create").disabled = false; $("create").textContent = "Create"; }
  };
}

// ---------------- twin detail ----------------
const TABS = ["spec", "invites", "coverage", "sessions", "imports", "ask"];

async function twinView(id, tab = "spec", extra) {
  const data = await call(`/api/twins/${id}/spec`);
  view.innerHTML = `
    <div class="stack">
      <div class="row"><h2 style="margin:0">${esc(data.twin.subject_name)}</h2><span class="pill">${esc(data.twin.status)}</span>${data.twin.simulated ? `<span class="pill sim" title="A simulated person (persona agent) took these interviews">simulated</span>` : ""}</div>
      <nav class="tabs">${TABS.map(t => `<button class="${t === tab ? "on" : ""}" data-tab="${t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</nav>
      <div id="pane"></div>
    </div>`;
  view.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => (location.hash = `#/twin/${id}/${b.dataset.tab}`));
  const pane = $("pane");
  const fns = { spec: specPane, invites: invitesPane, coverage: coveragePane, sessions: sessionsPane, imports: importsPane, ask: askPane };
  if (tab === "debug") return debugPane(pane, id, extra);
  await fns[tab](pane, id, data);
}

async function specPane(pane, id, data) {
  const latest = data.latest;
  pane.innerHTML = `
    <div class="stack">
      <p class="muted">Version ${latest.version} · ${latest.approved_at ? "approved" : "<b>not approved yet</b>"}.
        Edit the JSON if anything is off, save a new version, then approve it. Sessions use the newest approved version.</p>
      ${latest.brief_text ? `<details><summary>Original brief</summary><pre>${esc(latest.brief_text)}</pre></details>` : ""}
      <textarea id="spec" rows="24" spellcheck="false" style="font-family:var(--mono);font-size:.85rem">${esc(JSON.stringify(latest.spec, null, 2))}</textarea>
      <div class="row"><button id="save">Save as new version</button><button class="primary" id="approve">Approve latest</button></div>
    </div>`;
  $("save").onclick = async () => {
    try { await call(`/api/twins/${id}/spec`, { method: "PUT", body: { spec: JSON.parse($("spec").value) } }); route(); }
    catch (e) { err(e.message); }
  };
  $("approve").onclick = async () => { try { await call(`/api/twins/${id}/spec/approve`, { method: "POST" }); route(); } catch (e) { err(e.message); } };
}

async function invitesPane(pane, id) {
  const invites = await call(`/api/twins/${id}/invites`);
  pane.innerHTML = `
    <div class="stack">
      <div class="row"><input id="label" placeholder="Label (optional)" style="max-width:280px"><button class="primary" id="new">Create invite link</button></div>
      <div id="link"></div>
      <table><thead><tr><th>Label</th><th>Created</th><th>Expires</th><th></th></tr></thead><tbody>
      ${invites.map(i => `<tr><td>${esc(i.label || "–")}</td><td>${new Date(i.created_at).toLocaleString()}</td>
        <td>${i.revoked_at ? "revoked" : i.expires_at ? new Date(i.expires_at).toLocaleDateString() : "never"}</td>
        <td>${i.revoked_at ? "" : `<button class="danger" data-revoke="${i.id}">Revoke</button>`}</td></tr>`).join("")}
      </tbody></table>
      <p class="muted small">The link is shown once. Anyone with it can start a session, so share it only with the subject.</p>
    </div>`;
  $("new").onclick = async () => {
    try {
      const r = await call(`/api/twins/${id}/invites`, { method: "POST", body: { label: $("label").value || null } });
      $("link").innerHTML = `<div class="card"><b>Share this link:</b><pre id="lk">${esc(r.link)}</pre><button id="copy">Copy</button></div>`;
      $("copy").onclick = () => navigator.clipboard.writeText(r.link).catch(() => {});
      pane.querySelector("tbody").insertAdjacentHTML("afterbegin", `<tr><td>${esc($("label").value || "–")}</td>
        <td>${new Date().toLocaleString()}</td><td>${r.expires_at ? new Date(r.expires_at).toLocaleDateString() : "never"}</td>
        <td><button class="danger" data-revoke="${r.invite_id}">Revoke</button></td></tr>`);
      pane.querySelector(`[data-revoke="${r.invite_id}"]`).onclick = async () => {
        await call(`/api/twins/${id}/invites/${r.invite_id}/revoke`, { method: "POST" }); route();
      };
    } catch (e) { err(e.message); }
  };
  pane.querySelectorAll("[data-revoke]").forEach(b => b.onclick = async () => { await call(`/api/twins/${id}/invites/${b.dataset.revoke}/revoke`, { method: "POST" }); route(); });
}

async function coveragePane(pane, id) {
  const c = await call(`/api/twins/${id}/coverage`);
  const table = (rows, cols) => rows.length ? `<table><thead><tr>${cols.map(k => `<th>${k}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${cols.map(k => `<td>${esc(typeof r[k] === "object" ? JSON.stringify(r[k]) : r[k])}</td>`).join("")}</tr>`).join("")}</tbody></table>` : `<p class="muted">Nothing yet.</p>`;
  pane.innerHTML = `
    <div class="stack">
      <h3>Content slots</h3>${table(c.slots, ["domain_id", "item", "slot", "status", "n_evidence"])}
      <h3>Kinds of talk</h3>${table(c.registers, ["register", "modality", "word_count", "sessions", "done"])}
      <h3>Interests</h3>${table(c.interests, ["topic", "score", "n_obs"])}
      <h3>Never raise</h3>${table(c.do_not_raise, ["topic", "reason"])}
      <h3>Recent notes</h3>${table(c.notes, ["kind", "text"])}
    </div>`;
}

async function sessionsPane(pane, id) {
  const rows = await call(`/api/twins/${id}/sessions`);
  pane.innerHTML = rows.length ? `<table><thead><tr><th>Started</th><th>Mode</th><th>Status</th><th>Answers</th><th>Minutes</th><th></th></tr></thead><tbody>
    ${rows.map(s => `<tr><td>${new Date(s.started_at).toLocaleString()}</td><td>${s.modality}</td><td>${esc(s.status)}${s.end_reason ? ` <span class="muted small">(${esc(s.end_reason)})</span>` : ""}</td>
      <td>${s.turns}</td><td>${s.duration_s ? (s.duration_s / 60).toFixed(1) : "–"}</td><td><a href="#/twin/${id}/debug/${s.id}">${s.modality === "voice" ? "Listen &amp; inspect" : "Inspect"}</a></td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">No sessions yet. Create an invite link and share it.</p>`;
}

async function debugPane(pane, id, sid) {
  const [d, rec] = await Promise.all([call(`/api/twins/${id}/sessions/${sid}/debug`),
    call(`/api/twins/${id}/sessions/${sid}/recording`).catch(() => ({ parts: [] }))]);
  const LABELS = ["engaged", "flat", "hesitant", "laughing", "unclear"];
  pane.innerHTML = `
    <div class="stack">
      ${recordingPlayer(rec)}
      <p class="muted">Talk share (subject): <b>${d.talk_share == null ? "–" : Math.round(d.talk_share * 100) + "%"}</b> · target ≥ 75%</p>
      ${d.turns.map(t => `
        <div class="turn">
          <span class="who">${t.speaker} #${t.idx}${t.register ? ` · ${esc(t.register)}` : ""}</span>
          <div>${esc(t.text_live || "")}</div>
          ${t.text_corpus ? `<div class="small muted"><b>exact:</b> ${esc(t.text_corpus)}</div>` : ""}
          ${t.speaker === "subject" ? `
            ${t.decision ? `<div class="small"><span class="pill">${esc(t.decision.engagement)}</span> <b>${esc(t.decision.policy)}</b> — ${esc(t.decision.guidance)}</div>` : `<div class="small muted">not analyzed yet</div>`}
            ${t.observer ? `<details><summary>Observer output (${t.latency_ms} ms)</summary><pre>${esc(JSON.stringify(t.observer, null, 2))}</pre></details>` : ""}
            ${t.signals.length ? `<details><summary>Signals (${t.signals.length})</summary><pre>${esc(t.signals.map(s => `${s.source}/${s.kind}: ${JSON.stringify(s.value)}${s.confidence != null ? ` conf ${s.confidence}` : ""}`).join("\n"))}</pre></details>` : ""}
            <div class="row small">Label: ${LABELS.map(l => `<button data-turn="${t.id}" data-label="${l}" class="${t.label === l ? "primary" : ""}" style="padding:3px 10px">${l}</button>`).join("")}</div>` : ""}
        </div>`).join("")}
    </div>`;
  const players = [...pane.querySelectorAll("audio")];   // parts play on, one after another
  players.forEach((a, i) => { a.onended = () => players[i + 1]?.play(); fixDuration(a); swapChannels(a); });
  pane.querySelectorAll("[data-label]").forEach(b => b.onclick = async () => {
    await call("/api/labels", { method: "POST", body: { turn_id: b.dataset.turn, label: b.dataset.label } });
    pane.querySelectorAll(`[data-turn="${b.dataset.turn}"]`).forEach(x => x.classList.toggle("primary", x === b));
  });
}

// Browser recordings (MediaRecorder WebM) carry no duration, so the player shows a wrong
// length and progress until it has read the whole file. Ask it to seek to the end once:
// that makes it find the real length, then go back to the start.
function fixDuration(a) {
  a.addEventListener("loadedmetadata", () => {
    if (Number.isFinite(a.duration)) return;
    a.addEventListener("durationchange", function back() {
      if (!Number.isFinite(a.duration)) return;
      a.removeEventListener("durationchange", back);
      a.currentTime = 0;
    });
    a.currentTime = 1e101;
  }, { once: true });
}

// Early simulator recordings have the person on the left; play them interviewer-left too.
function swapChannels(a) {
  if (!("swap" in a.dataset)) return;
  a.addEventListener("play", () => {
    if (a._swapped) return;
    a._swapped = true;
    const ctx = new AudioContext();
    const split = ctx.createChannelSplitter(2), merge = ctx.createChannelMerger(2);
    ctx.createMediaElementSource(a).connect(split);
    split.connect(merge, 0, 1);
    split.connect(merge, 1, 0);
    merge.connect(ctx.destination);
  });
}

function recordingPlayer(rec) {
  if (!rec.parts.length) return `<p class="muted small">No recording of the whole call for this session (calls before the recorder existed only kept their microphone, for transcription).</p>`;
  const many = rec.parts.length > 1;
  return `<div class="card stack" style="gap:6px">
      <b>Listen to the interview</b>
      ${rec.parts.map((p, i) => `${many ? `<span class="small muted">Part ${i + 1}</span>` : ""}
        <audio controls preload="metadata" crossorigin="anonymous" style="width:100%" src="${esc(p.url)}"${rec.swap ? " data-swap" : ""}></audio>`).join("")}
      <span class="small muted">With headphones: the interviewer on the left, ${rec.source === "simulator" ? "the simulated person" : "the person"} on the right.</span>
    </div>`;
}

async function importsPane(pane, id) {
  pane.innerHTML = `
    <div class="stack" style="max-width:520px">
      <p class="muted">Add existing writing by the subject, like a WhatsApp chat export (.txt). Only their own messages are kept.</p>
      <label for="kind">Kind</label>
      <select id="kind"><option value="whatsapp">WhatsApp export</option><option value="email">Emails (plain text)</option><option value="doc">Document (plain text)</option></select>
      <label for="author">Their name exactly as it appears in the chat (WhatsApp only)</label><input id="author">
      <input id="file" type="file" accept=".txt,text/plain">
      <div><button class="primary" id="up">Upload</button> <span id="upst" class="muted"></span></div>
    </div>`;
  $("up").onclick = async () => {
    const f = $("file").files[0];
    if (!f) return err("Choose a file first.");
    try {
      $("upst").textContent = "Uploading…";
      const r = await call(`/api/twins/${id}/imports`, { method: "POST", body: { kind: $("kind").value, filename: f.name, author_name: $("author").value || null } });
      const put = await fetch(r.upload.url, { method: "PUT", headers: r.upload.headers, body: f });
      if (!put.ok) throw new Error(`Upload failed (${put.status})`);
      await call(`/api/twins/${id}/imports/${r.import_id}/complete`, { method: "POST" });
      $("upst").textContent = "Added. It's being processed.";
    } catch (e) { err(e.message); $("upst").textContent = ""; }
  };
}

async function askPane(pane, id) {
  pane.innerHTML = `
    <div class="stack" style="max-width:640px">
      <p class="muted">Ask the rough twin a question. It's an evaluation tool, built only from what's been collected so far.</p>
      <textarea id="q" rows="2" placeholder="How do you make your dal?"></textarea>
      <div><button class="primary" id="ask">Ask</button></div>
      <div id="a"></div>
    </div>`;
  $("ask").onclick = async () => {
    $("a").innerHTML = `<p class="muted">Thinking…</p>`;
    try { const r = await call(`/api/twins/${id}/ask`, { method: "POST", body: { question: $("q").value } }); $("a").innerHTML = `<div class="card">${esc(r.answer)}</div>`; }
    catch (e) { $("a").innerHTML = ""; err(e.message); }
  };
}

async function route() {
  err("");
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  try {
    if (parts[0] === "twin") await twinView(parts[1], parts[2] || "spec", parts[3]);
    else await home();
  } catch (e) { err(e.message); }
}

window.addEventListener("hashchange", route);
if (await auth()) route(); else signInView();
