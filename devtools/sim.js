// Interview simulator (dev only; docs/simulator.md).
// Persona = its own GPT-Live session. The real voice page runs in the iframe; we hand it the
// persona's voice as its "microphone" and route its interviewer audio into the persona.
import { api as baseApi, esc, sleep } from "/common.js";

const $ = id => document.getElementById(id);
const SIM_CREATOR = "00000000-0000-0000-0000-0000000051a0";
const MOOD_COLOR = { excited: "#E35A0A", smiling: "#ED4C7D", bored: "#75726C", exhausted: "#5E6AEC" };
let personas = [], running = false, ctx;
// On the deployed app the simulator needs its key (?key=...): sent on every simulator call.
const KEY = new URLSearchParams(location.search).get("key") || "";
const withKey = url => (KEY ? `${url}${url.includes("?") ? "&" : "?"}key=${encodeURIComponent(KEY)}` : url);
const api = (path, opts = {}) => baseApi(path, { ...opts, headers: { ...(opts.headers || {}), ...(KEY ? { "x-sim-key": KEY } : {}) } });

const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
function line(kind, who, text, t) {
  const log = $("log");
  if (log.querySelector("p.muted")) log.innerHTML = "";
  const el = document.createElement("div");
  el.className = `line ${kind}`;
  el.innerHTML = `<span class="t">${t != null ? fmt(t) : ""}</span><span class="who">${esc(who)}</span><span>${text}</span>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}
const state = t => ($("state").textContent = t);

async function load() {
  personas = await api("/api/dev/personas");
  $("personas").innerHTML = personas.map(p => `
    <label class="persona"><input type="checkbox" value="${esc(p.id)}" checked>
      <div><b>${esc(p.name)}</b> <span class="small muted">· ${Math.round(p.run_seconds / 60)} min · voice ${esc(p.voice)}</span>
        <div class="moods">${p.reactive && !scripted()
          ? `<span class="pill" style="color:${MOOD_COLOR[moodFor(p.reactive.start, false)]}">starts ${esc(moodFor(p.reactive.start, false))}, energy ${p.reactive.start}/10</span><span class="small muted">reacts to each question</span>`
          : p.phases.map(ph => `<span class="pill" style="color:${MOOD_COLOR[ph.mood] || "inherit"}">${fmt(ph.at_s)} ${esc(ph.mood)}</span>`).join("")}</div>
      </div></label>`).join("");
  const updateCost = () => {
    const mins = [...document.querySelectorAll("#personas input:checked")]
      .map(i => personas.find(p => p.id === i.value).run_seconds / 60).reduce((a, b) => a + b, 0);
    $("cost").textContent = mins ? `~${Math.round(mins)} min, ~$${(mins * 0.1 + 0.2).toFixed(2)} (two live sessions)` : "";
  };
  $("personas").addEventListener("change", updateCost);
  updateCost();
}

// One live transcript stream (deltas) folded into readable lines; each line also goes into
// `convo` ({who, text}) so the reactive director can show the judge the latest exchange.
function streamer(kind, who, elapsedMs, convo) {
  let el = null, text = "", entry = null;
  const s = {
    last: 0,
    add(delta) {
      const now = Date.now();
      if (!el || now - s.last > 1500) {
        text = ""; el = line(kind, who, "", elapsedMs() / 1000);
        entry = { who: kind === "ai" ? "interviewer" : "them", text: "" };
        convo?.push(entry);
      }
      text += delta; s.last = now;
      el.lastElementChild.textContent = entry.text = text.trim();
    },
  };
  return s;
}

// Same bands as app/routers/dev.py mood_for
const moodFor = (energy, tired) => energy <= 3 ? (tired ? "exhausted" : "bored") : energy >= 7 ? "excited" : "smiling";
const scripted = () => new URLSearchParams(location.search).has("scripted");

async function runOne(pid) {
  const p = structuredClone(personas.find(x => x.id === pid));
  // ?seconds=90 squeezes the run (and its mood phases) for a quick wiring check
  const short = Number(new URLSearchParams(location.search).get("seconds"));
  if (short > 0) {
    const k = short / p.run_seconds;
    p.phases.forEach(ph => (ph.at_s = Math.round(ph.at_s * k)));
    if (p.reactive) p.reactive.stamina_s = Math.round(p.reactive.stamina_s * k);
    p.run_seconds = short;
  }
  state(`${p.name}: setting up`);
  line("sys", "setup", `Creating ${esc(p.name)}'s twin from the brief (real brief compiler)…`);
  const setup = await api("/api/dev/sim/setup", { method: "POST", body: { persona: pid } });

  // --- audio graph -------------------------------------------------------------------------
  const noise = dest => {   // a faint tone = a mic's noise floor; GPT-Live closes on dead air
    const o = ctx.createOscillator(), g = ctx.createGain();
    g.gain.value = 0.0005; o.connect(g).connect(dest); o.start();
    return o;
  };
  const toInterviewer = ctx.createMediaStreamDestination();   // persona voice → "mic"
  const toPersona = ctx.createMediaStreamDestination();       // interviewer voice → persona
  const tones = [noise(toInterviewer), noise(toPersona)];
  // Recording for review: stereo, interviewer left (0), persona right (1).
  const merger = ctx.createChannelMerger(2);
  const recDest = ctx.createMediaStreamDestination();
  merger.connect(recDest);
  const recChunks = [];
  const recorder = new MediaRecorder(recDest.stream, { mimeType: "audio/webm;codecs=opus" });
  recorder.ondataavailable = e => e.data.size && recChunks.push(e.data);
  const keepAlive = [];
  const pipe = (stream, dest, channel) => {
    const a = new Audio(); a.srcObject = stream; a.muted = true; a.play().catch(() => {});  // Chrome needs a sink
    keepAlive.push(a);
    const src = ctx.createMediaStreamSource(stream);
    src.connect(dest);
    src.connect(merger, 0, channel);
    if ($("listen").checked) src.connect(ctx.destination);
  };

  // --- persona session -----------------------------------------------------------------------
  let T0 = null;                       // epoch ms of the interviewer's mic start (its clock t0)
  const since = () => (T0 ? Date.now() - T0 : 0);
  const run = { said: [], heard: [], phases: [], notes: [], energy: [] };
  const convo = [];
  const pc = new RTCPeerConnection();
  pc.addTrack(toPersona.stream.getAudioTracks()[0], toPersona.stream);
  pc.ontrack = e => pipe(e.streams[0], toInterviewer, 1);
  const dc = pc.createDataChannel("oai-events");
  const said = streamer("persona-line", p.name, since, convo), heard = [];
  dc.onmessage = msg => {
    let e; try { e = JSON.parse(msg.data); } catch { return; }
    if (e.type === "session.output_transcript.delta") { said.add(e.delta || ""); run.said.push({ ms: since(), d: e.delta }); }
    else if (e.type === "session.input_transcript.delta") { heard.push(e.delta || ""); run.heard.push({ ms: since(), d: e.delta }); }
    else if (e.type === "session.delegation.created")
      dc.send(JSON.stringify({ type: "session.commentary.append", delegation_id: e.delegation?.id, content: "Just answer as yourself." }));
    else if (e.type === "error") run.notes.push(`persona error: ${e.error?.code}`);
    else if (e.type === "session.closed") run.notes.push(`persona closed: ${e.reason}`);
  };
  await pc.setLocalDescription(await pc.createOffer());
  const pans = await api("/api/dev/persona/connect", { method: "POST", body: { persona: pid, sdp: pc.localDescription.sdp } });
  await pc.setRemoteDescription({ type: "answer", sdp: pans.sdp });

  // --- the real voice page, with the persona as its microphone ---------------------------------
  const frame = $("frame");
  frame.src = `/s/${setup.token}/voice`;
  await new Promise(r => (frame.onload = r));
  const w = frame.contentWindow, d = frame.contentDocument;
  w.navigator.mediaDevices.getUserMedia = async () => {
    T0 = Date.now();
    recorder.start(5000);
    return toInterviewer.stream;
  };
  // the interviewer's audio lands on #remote; route it into the persona's ears
  const watch = setInterval(() => {
    const el = d.getElementById("remote");
    if (el && el.srcObject && !el.dataset.piped) { el.dataset.piped = "1"; pipe(el.srcObject, toPersona, 0); }
  }, 200);
  // interviewer transcript: read it off GPT-Live's own events inside the page
  const ai = streamer("ai", "interviewer", since, convo);
  const OrigPC = w.RTCPeerConnection;
  w.RTCPeerConnection = function (...args) {
    const inner = new OrigPC(...args);
    const create = inner.createDataChannel.bind(inner);
    inner.createDataChannel = (...a) => {
      const ch = create(...a);
      ch.addEventListener("message", m => {
        try { const e = JSON.parse(m.data); if (e.type === "session.output_transcript.delta") ai.add(e.delta || ""); } catch { /* ignore */ }
      });
      return ch;
    };
    return inner;
  };
  d.getElementById("start").click();
  state(`${p.name}: interviewing`);

  const cue = text => dc.readyState === "open" &&
    dc.send(JSON.stringify({ type: "session.instructions.append", delegation_id: null, content: text }));
  const moodLine = (mood, s, extra = "") => line("sys", "mood",
    `<span class="phase" style="color:${MOOD_COLOR[mood]}">${esc(mood)}</span>${extra}`, s).classList.add("sys");

  while (!T0) await sleep(100);
  let director;
  if (p.reactive && !scripted()) {
    // --- reactive director: each interviewer turn, once finished, is judged as the persona;
    // their energy (0-10) moves with it and sets their mood ----------------------------------
    let energy = p.reactive.start, mood = moodFor(energy, false), judged = 0, busy = false;
    run.phases.push({ mood, at_ms: 0 });
    moodLine(mood, 0, ` <span class="small muted">energy ${energy}/10</span>`);
    // "Mm.", "Okay.": listening noises while they're mid-story, not turns to judge (same rule
    // as is_backchannel in scripts/sim_score.py)
    const backchannel = t => t.split(/\s+/).filter(Boolean).length <= 2 && !t.includes("?");
    director = setInterval(async () => {
      const turns = convo.filter(x => x.who === "interviewer" && !backchannel(x.text)).length;
      if (busy || turns <= judged || Date.now() - ai.last < 1800) return;
      busy = true; judged = turns;
      const ms = since();
      try {
        const r = await api("/api/dev/persona/react", { method: "POST", body: {
          persona: pid, energy, elapsed_s: ms / 1000, exchange: convo.slice(-8) } });
        energy = r.energy;
        run.energy.push({ ms, energy: r.energy, effect: r.effect, fatigue: r.fatigue, reason: r.reason,
                          interviewer: convo.filter(x => x.who === "interviewer" && !backchannel(x.text)).at(-1)?.text || "" });
        const sign = r.effect > 0 ? `+${r.effect}` : `${r.effect}`;
        line("sys", "lands", `${sign}${r.fatigue ? ` −${r.fatigue} tired` : ""} → ${r.energy.toFixed(1)}/10 · ${esc(r.reason)}`, ms / 1000).classList.add("sys");
        if (r.mood !== mood) {
          mood = r.mood;
          run.phases.push({ mood, at_ms: ms });
          if (r.cue) cue(r.cue);
          moodLine(mood, ms / 1000);
        }
      } catch (e) { run.notes.push(`react failed: ${e.message || e}`); }
      busy = false;
    }, 500);
  } else {
    // --- scripted director (?scripted=1): mood phases on the interview clock ----------------
    let next = 0;
    director = setInterval(() => {
      const s = since() / 1000;
      while (next < p.phases.length && s >= p.phases[next].at_s) {
        const ph = p.phases[next];
        run.phases.push({ mood: ph.mood, at_ms: since() });
        if (next > 0) cue(ph.cue);
        moodLine(ph.mood, s);
        next++;
      }
    }, 500);
  }

  // --- run until time is up or the page ends itself ------------------------------------------
  const ended = () => /Thanks|Saved|Saving/.test(d.getElementById("status")?.textContent || "");
  while (since() / 1000 < p.run_seconds && !ended()) await sleep(1000);
  clearInterval(director);
  line("sys", "end", ended() ? "The platform ended the call." : "Time's up: pressing Finish on the voice page.", since() / 1000);
  if (!ended()) d.getElementById("stop").click();
  for (let i = 0; i < 60 && !/Thanks|Saved/.test(d.getElementById("status")?.textContent || ""); i++) await sleep(1000);
  dc.readyState === "open" && dc.send(JSON.stringify({ type: "session.close" }));
  await sleep(1500);
  await new Promise(r => { recorder.onstop = r; recorder.state !== "inactive" ? recorder.stop() : r(); });
  clearInterval(watch); pc.close(); tones.forEach(o => o.stop()); keepAlive.forEach(a => (a.srcObject = null));

  // --- save the run for scoring ----------------------------------------------------------------
  const { session_id: sid } = await api(`/api/dev/sim/twins/${setup.twin_id}/latest-session`);
  await api("/api/dev/sim/runs", { method: "POST", body: {
    session_id: sid, twin_id: setup.twin_id, persona: pid, t0_epoch_ms: T0,
    phases: run.phases, persona_said: run.said, persona_heard: run.heard, notes: run.notes,
    energy: run.energy } });
  const blob = new Blob(recChunks, { type: "audio/webm" });
  const up = await api(`/api/dev/sim/runs/${sid}/recording-upload`, { method: "POST" });
  await fetch(up.url, { method: up.method, headers: up.headers, body: blob });
  await renderRuns();
  line("sys", "saved", `Run saved. Score it: <code>uv run python scripts/sim_score.py ${esc(sid)}</code>`);
  window.__simRuns = [...(window.__simRuns || []), { persona: pid, session_id: sid, twin_id: setup.twin_id }];
}

async function renderRuns() {
  const runs = await api("/api/dev/sim/runs");
  const name = id => (personas.find(p => p.id === id) || { name: id }).name;
  $("results").innerHTML = `<h3>Runs</h3>` + (runs.length ? runs.map(r => `
    <div class="stack" style="gap:6px;padding-top:10px;border-top:1px solid var(--rule)">
      <div class="row" style="justify-content:space-between">
        <b>${esc(name(r.persona))}</b>
        <span class="small muted">${new Date(r.saved_at * 1000).toLocaleString()}</span>
      </div>
      ${r.recording ? `<audio controls preload="none" style="width:100%" src="${withKey(`/api/dev/sim/runs/${r.session_id}/recording`)}"></audio>
        <span class="small muted">Left ear: interviewer · right ear: ${esc(name(r.persona))}</span>` : `<span class="small muted">No recording</span>`}
      <div class="row small">
        <a href="/creator${KEY ? "" : `?as=${SIM_CREATOR}`}#/twin/${r.twin_id}/sessions" target="_blank">debug view</a>
        ${r.scorecard ? `<a href="${withKey(`/api/dev/sim/runs/${r.session_id}/scorecard`)}" target="_blank">scorecard</a>` : `<span class="muted">not scored yet</span>`}
      </div>
    </div>`).join("") : `<p class="small muted">None yet.</p>`);
}

$("run").addEventListener("click", async () => {
  if (running) return;
  running = true; $("run").disabled = true;
  ctx = ctx || new AudioContext({ sampleRate: 48000 });
  await ctx.resume();
  const ids = [...document.querySelectorAll("#personas input:checked")].map(i => i.value);
  let failed = 0;
  for (const id of ids) {
    // One failed run must not stop the rest of the batch.
    try { await runOne(id); } catch (e) { failed += 1; line("sys", "error", `${esc(id)}: ${esc(e.message || String(e))}`); }
  }
  state(failed ? `done, ${failed} failed` : "done");
  running = false; $("run").disabled = false;
});

load().then(renderRuns);
