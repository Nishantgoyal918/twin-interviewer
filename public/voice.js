// Voice session (plan 8.2, 7.9, 7.10).
// Browser ↔ GPT-Live over WebRTC; our API only creates the session. Session clock t0 = mic start.
import { api, apiRetry, inviteToken, uid, sleep } from "/common.js";
import { UploadQueue } from "/uploads.js";

const $ = id => document.getElementById(id);
const status = t => ($("status").textContent = t);
const showError = m => { $("error").textContent = m || ""; $("error").hidden = !m; };

let session, stream, pc, dc, t0 = 0, liveStartedAt = null;
let ended = false, segmentsStarted = 0;
let uploads;

// ---------------- session clock ----------------
const now = () => Math.round(performance.now() - t0);
// GPT-Live delta times are relative to its session start; shift onto our clock.
const toClock = ms => (typeof ms === "number" && liveStartedAt !== null) ? Math.round(ms + liveStartedAt) : now();

// ---------------- VAD + live features (Web Audio) ----------------
const FRAME_MS = 50;
const frames = [];            // {t, db}
let noiseFloor = -60;
function startVad(mic) {
  const ctx = new AudioContext();
  const src = ctx.createMediaStreamSource(mic);
  const an = ctx.createAnalyser();
  an.fftSize = 1024;
  src.connect(an);
  const buf = new Float32Array(an.fftSize);
  setInterval(() => {
    an.getFloatTimeDomainData(buf);
    let sum = 0;
    for (const x of buf) sum += x * x;
    const db = 10 * Math.log10(sum / buf.length + 1e-10);
    frames.push({ t: now(), db });
    if (frames.length > 20 * 60 * 15) frames.splice(0, 1200);
    // slow-moving noise floor estimate
    noiseFloor = db < noiseFloor ? db : noiseFloor + 0.01;
    const level = Math.max(0, Math.min(1, (db - noiseFloor) / 30));
    $("orb").style.transform = `scale(${1 + level * 0.15})`;
  }, FRAME_MS);
}
const voicedAt = db => db > noiseFloor + 12;

function features(start, end, prevOutputEnd) {
  const win = frames.filter(f => f.t >= start - 200 && f.t <= end + 200);
  if (!win.length) return {};
  const voiced = win.filter(f => voicedAt(f.db));
  const dbs = voiced.map(f => f.db);
  const mean = dbs.length ? dbs.reduce((a, b) => a + b, 0) / dbs.length : 0;
  const variance = dbs.length ? dbs.reduce((a, b) => a + (b - mean) ** 2, 0) / dbs.length : 0;
  const pauses = [];
  let run = 0, seenVoice = false;
  for (const f of win) {
    if (voicedAt(f.db)) { if (seenVoice && run * FRAME_MS >= 300) pauses.push(run * FRAME_MS); run = 0; seenVoice = true; }
    else if (seenVoice) run++;
  }
  const out = {
    voiced_s: voiced.length * FRAME_MS / 1000,
    energy_mean_db: mean,
    energy_var: variance,
    pause_count: pauses.length,
    pause_mean_s: pauses.length ? pauses.reduce((a, b) => a + b, 0) / pauses.length / 1000 : 0,
  };
  if (prevOutputEnd != null && voiced.length) out.ttfv_s = Math.max(0, (voiced[0].t - prevOutputEnd) / 1000);
  return out;
}

// ---------------- rotating recorders (7.9) ----------------
function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"])
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
  return "";
}
const recorders = new Set();
function startSegment(seq) {
  if (ended) return;
  segmentsStarted = Math.max(segmentsStarted, seq + 1);
  const mime = pickMime();
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  const startMs = now();
  rec.ondataavailable = e => e.data.size && chunks.push(e.data);
  rec.onstop = () => {
    recorders.delete(rec);
    const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
    uploads.add({ seq, start_ms: startMs, end_ms: now(), blob });
  };
  rec.start();
  recorders.add(rec);
  const segS = session.segment_seconds, overlapS = session.overlap_seconds;
  setTimeout(() => startSegment(seq + 1), (segS - overlapS) * 1000);
  setTimeout(() => rec.state !== "inactive" && rec.stop(), segS * 1000);
}

// ---------------- whole-call recording (both voices) ----------------
// The segments above are their microphone only, for transcription. For listening back, also
// record the call as heard: the interviewer on the left, them on the right. Long calls are
// cut into parts (each a playable file) so a dropped tab loses one part, not the whole call.
const CALL_PART_S = 600;
let callMix = null;
function startCallRecording(remote) {
  if (callMix || ended) return;
  const ctx = new AudioContext();
  const merger = ctx.createChannelMerger(2);
  ctx.createMediaStreamSource(remote).connect(merger, 0, 0);
  ctx.createMediaStreamSource(stream).connect(merger, 0, 1);
  const dest = ctx.createMediaStreamDestination();
  merger.connect(dest);
  callMix = dest.stream;
  startCallPart(0);
}
function startCallPart(seq) {
  if (ended) return;
  const mime = pickMime();
  const rec = new MediaRecorder(callMix, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  const startMs = now();
  rec.ondataavailable = e => e.data.size && chunks.push(e.data);
  rec.onstop = () => {
    recorders.delete(rec);
    const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
    if (blob.size) uploads.add({ kind: "call", seq, start_ms: startMs, end_ms: now(), blob });
  };
  rec.start(1000);
  recorders.add(rec);
  setTimeout(() => { if (!ended && rec.state !== "inactive") { rec.stop(); startCallPart(seq + 1); } },
             CALL_PART_S * 1000);
}

// ---------------- turn grouping (no turn-done events exist) ----------------
let phase = "idle";
let subj = { text: "", start: null, end: null };
let out = { text: "", start: null, end: null };
let lastOutputEnd = null;
let lastGuidance = "", questionsSinceTip = 0;

// ---------------- dead air ----------------
// If the interviewer ends without a question and they wait (or they're stuck), nobody speaks.
// After a quiet spell, cue the interviewer once to pick it up gently. Activity = transcribed
// words from either side, not mic level: fans, TVs and comfort noise never produce words. Longer wait after a
// question: slow speakers need time to think. At most 2 cues until they speak again.
const QUIET_AFTER_STATEMENT_S = 7, QUIET_AFTER_QUESTION_S = 15, MAX_QUIET_CUES = 2;
let lastActivity = 0, quietCues = 0, wrapping = false;

function checkQuiet() {
  if (wrapping || liveStartedAt === null || quietCues >= MAX_QUIET_CUES) return;
  const asked = /\?\s*$/.test(out.text.trim() || lastInterviewerText);
  const limit = (asked ? QUIET_AFTER_QUESTION_S : QUIET_AFTER_STATEMENT_S) * 1000;
  if (now() - Math.max(lastActivity, liveStartedAt) < limit) return;
  quietCues += 1;
  lastActivity = now();
  send({ type: "session.commentary.append", delegation_id: null, content: asked
    ? "They've been quiet for a while. Gently offer an easier way in: a simpler question, or a choice between two things. Don't mention the silence."
    : "It's gone quiet and you didn't leave them a question. Keep the conversation going with one short, easy question. Don't mention the silence." });
}
let lastInterviewerText = "";

// ---------------- hanging up ----------------
// Once wrapping, the interviewer's first turn without a question is its farewell. A warm
// person says thanks back, the interviewer thanks them again, and nobody ever hangs up. So
// after the farewell: let them answer and the interviewer reply once, or let it go quiet,
// then end the call. If they start something real (a long answer), keep talking once.
const OUTPUT_DONE_MS = 1500, AFTER_FAREWELL_QUIET_MS = 8000, LET_AUDIO_FINISH_MS = 2500;
const STILL_TALKING_WORDS = 15;
let lastOutputAt = 0, outputTurnOpen = false, farewellAt = null, repliesAfterFarewell = 0;
let farewellResets = 0;

function reopen() {
  if (farewellResets >= 1) return;   // once only: the second farewell ends the call
  farewellResets += 1;
  farewellAt = null;
  repliesAfterFarewell = 0;
}

// The interviewer may say goodbye on its own, before any wrap cue. Then the call is ending:
// without this, the dead-air cue makes it ask a fresh question right after "Bye, take care!".
const GOODBYE = /\b(bye|goodbye|take care)\b/i;

function checkHangUp() {
  if (!wrapping && outputTurnOpen && now() - lastOutputAt > OUTPUT_DONE_MS) {
    const said = lastInterviewerText.trim();
    if (GOODBYE.test(said) && !said.includes("?")) wrapping = true;
  }
  if (!wrapping || ended) return;
  const t = now();
  if (outputTurnOpen && t - lastOutputAt > OUTPUT_DONE_MS) {   // an interviewer turn just ended
    outputTurnOpen = false;
    const said = lastInterviewerText.trim();
    const short = said.split(/\s+/).length <= 2 && !said.includes("?");
    if (farewellAt === null && short) return;   // "Mm." isn't a farewell; after one, "Bye!" is the reply
    const asked = /\?\s*$/.test(said);
    if (farewellAt === null) {
      if (!asked) farewellAt = t;
    } else if (asked) {
      reopen();                      // it asked something new after all: let them answer
    } else {
      repliesAfterFarewell += 1;
    }
  }
  if (farewellAt === null || outputTurnOpen) return;
  // Mid-sentence with more than a goodbye: let them finish (and maybe reopen).
  if (phase === "input" && subj.text.trim().split(/\s+/).length > 6) return;
  // After the interviewer has answered their goodbye, don't wait for silence: two polite
  // people trade "Bye." forever. Hang up once its last words have played.
  const done = repliesAfterFarewell >= 1
    ? t - lastOutputAt > LET_AUDIO_FINISH_MS
    : t - Math.max(lastActivity, lastOutputAt) > AFTER_FAREWELL_QUIET_MS;
  if (done) {
    status("Ending the call…");
    finish("planner_wrap");
  }
}

function onInputDelta(e) {
  lastActivity = now();
  quietCues = 0;
  if (phase !== "input") { phase = "input"; subj = { text: "", start: toClock(e.start_ms), end: null }; }
  subj.text += e.delta || "";
  subj.end = toClock(e.end_ms);
  $("caption").textContent = subj.text.slice(-160);
}

function onOutputDelta(e) {
  if (phase === "input") finalizeSubjectTurn();
  if (phase !== "output") { phase = "output"; if (!out.text) out.start = toClock(e.start_ms); }
  out.text += e.delta || "";
  out.end = toClock(e.end_ms);
  lastActivity = lastOutputAt = now();
  outputTurnOpen = true;
  lastInterviewerText = out.text;
  lastOutputEnd = out.end;
  $("caption").textContent = out.text.slice(-160);
}

async function finalizeSubjectTurn() {
  const s = subj, o = out;
  const words = s.text.trim().split(/\s+/).filter(Boolean);
  const dur = (s.end ?? now()) - (s.start ?? now());
  if (!words.length) return;
  if (words.length <= 2 && dur < 1000) return;   // backchannel: keep the interviewer turn open
  if (farewellAt !== null && words.length > STILL_TALKING_WORDS) reopen();   // more to say
  out = { text: "", start: null, end: null };
  const body = {
    client_turn_id: uid(),
    interviewer: o.text ? { text: o.text.trim(), start_ms: o.start, end_ms: o.end } : null,
    subject: { text: s.text.trim(), start_ms: s.start, end_ms: s.end },
    features: features(s.start, s.end ?? now(), o.end),
  };
  try {
    const res = await apiRetry("/api/turn", { method: "POST", body, token: session.session_token });
    if (res.wrap_up) wrapping = true;
    if (res.wrap_up) sendThinking("Time is nearly up. Wrap up warmly at the next natural moment: one goodbye turn with no question. If they say thanks or bye back, answer with a few words at most; the call ends by itself.");
    const a = await apiRetry(`/api/turn/${res.turn_id}/analyze`, { method: "POST", token: session.session_token });
    if (o.text.includes("?")) questionsSinceTip += 1;
    // A firm tip (time to move to a new topic) is repeated after each question that didn't
    // make the move: said once, it fades behind the conversation. Soft tips go once.
    const again = a.firm && questionsSinceTip >= 1;
    if (a.guidance && (a.guidance !== lastGuidance || again) && !wrapping) {   // none after goodbye
      lastGuidance = a.guidance;
      questionsSinceTip = 0;
      sendThinking((a.firm ? "Coach (do this with your next question, once their current thought ends): "
                           : "Coach's tip: ") + a.guidance);
    }
  } catch (e) {
    console.warn("turn post failed", e);   // backlog job analyzes later
  }
}

function send(evt) { if (dc && dc.readyState === "open") dc.send(JSON.stringify({ event_id: uid(), ...evt })); }
// Silent context: never spoken, never interrupts (session.thinking.append, delegation_id null).
function sendThinking(content) { send({ type: "session.thinking.append", delegation_id: null, content: content.slice(0, 1800) }); }

async function onDelegation(e) {
  const id = e.delegation?.id;
  try {
    const res = await api("/api/delegation", { method: "POST", token: session.session_token,
      body: { delegation_id: id, last_subject_text: subj.text.slice(-1000) } });
    send({ type: "session.commentary.append", delegation_id: id, content: res.content });
  } catch (err) {
    send({ type: "session.commentary.append", delegation_id: id, content: "Nothing new; keep following their lead." });
  }
}

function onEvent(msg) {
  let e;
  try { e = JSON.parse(msg.data); } catch { return; }
  switch (e.type) {
    case "session.started":
      liveStartedAt = now();
      // Backup nudge for the greeting set at creation (GPT-Live guide: both are recommended).
      send({ type: "session.commentary.append", delegation_id: null,
             content: "Greet them now and open the conversation as instructed, then listen." });
      status("Listening. Speak whenever you like.");
      $("orb").textContent = "listening";
      break;
    case "session.input_transcript.delta": onInputDelta(e); break;
    case "session.output_transcript.delta": onOutputDelta(e); break;
    case "session.delegation.created": onDelegation(e); break;
    case "session.closed": if (!ended) finish("remote"); break;
    case "error": console.warn("GPT-Live error", e.error); break;
  }
}

// ---------------- lifecycle ----------------
async function begin() {
  $("start").disabled = true;
  showError("");
  try {
    session = await api("/api/sessions", { method: "POST", body: { invite_token: inviteToken(), modality: "voice" } });
    uploads = new UploadQueue(session);
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    t0 = performance.now();
    startVad(stream);
    startSegment(0);

    pc = new RTCPeerConnection();
    pc.ontrack = ev => { $("remote").srcObject = ev.streams[0]; startCallRecording(ev.streams[0]); };
    stream.getTracks().forEach(t => pc.addTrack(t, stream));
    dc = pc.createDataChannel("oai-events");
    dc.onmessage = onEvent;
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitIce(pc);
    status("Connecting…");
    const ans = await api("/api/voice/connect", { method: "POST", token: session.session_token, body: { sdp: pc.localDescription.sdp } });
    await pc.setRemoteDescription({ type: "answer", sdp: ans.sdp });
    $("start").hidden = true;
    $("stop").hidden = false;
    tick();
  } catch (e) {
    showError(e.message || String(e));
    $("start").disabled = false;
    if (session && !ended) finish("error");
  }
}

function waitIce(peer) {
  if (peer.iceGatheringState === "complete") return Promise.resolve();
  return new Promise(res => {
    const done = () => peer.iceGatheringState === "complete" && res();
    peer.addEventListener("icegatheringstatechange", done);
    setTimeout(res, 3000);
  });
}

let warned = false;
function tick() {
  if (ended) return;
  const elapsed = session.elapsed_s + now() / 1000;
  const left = Math.max(0, session.max_duration_s - elapsed);
  $("timer").textContent = `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")} left`;
  checkQuiet();
  checkHangUp();
  if (ended) return;
  if (left <= 0 && !warned) { warned = true; wrapping = true; sendThinking("Time is up. Say a warm, short goodbye now."); }
  if (left <= -60) { finish("max_duration"); return; }
  setTimeout(tick, 1000);
}

async function finish(reason) {
  if (ended) return;
  ended = true;
  status("Saving…");
  $("stop").disabled = true;
  if (phase === "input") await finalizeSubjectTurn();
  send({ type: "session.close" });
  for (const r of recorders) if (r.state !== "inactive") r.stop();
  await sleep(300);
  try { pc && pc.close(); } catch { /* closed */ }
  stream && stream.getTracks().forEach(t => t.stop());
  try {
    await api(`/api/sessions/${session.session_id}/end`, { method: "POST", token: session.session_token,
      body: { reason: reason === "remote" ? "user" : reason, expected_segments: segmentsStarted } });
  } catch { /* the daily sweep ends it */ }
  await uploads.drain(30000);
  status(uploads.pending() ? "Saved. Some audio is still uploading; keep this page open a little longer if you can."
                           : "Thanks! Everything is saved.");
  $("orb").textContent = "done";
}

$("start").addEventListener("click", begin);
$("stop").addEventListener("click", () => finish("user"));
window.addEventListener("pagehide", () => {
  if (!session || ended) return;
  navigator.sendBeacon(`/api/sessions/${session.session_id}/end`,
    JSON.stringify({ session_token: session.session_token, reason: "tab_closed", expected_segments: segmentsStarted }));
});

// Leftover uploads from an earlier visit on this device.
UploadQueue.retryStored();
