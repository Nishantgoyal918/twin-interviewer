# Decisions and open checks

## Decisions

The plan's v3 section 16 lists all 24 fixes. The ones that shaped the code:

| Decision | Why |
|---|---|
| Fast `/turn` + background `/analyze` | reply latency doesn't wait for the observer |
| `idx` assigned in Postgres under a Redis lock; `client_turn_id` unique | retries and parallel tabs can't create duplicate or out-of-order turns |
| Planner is plain code | predictable, testable, free |
| Observer's `lexical_engagement` replaces a separate tone judge | one LLM call per turn instead of two |
| Baselines per source (text, VAD, Deepgram timing), Welford running stats | features from different sources aren't comparable |
| Cold-start confidence cap 0.4 | don't act on signals before a baseline exists |
| Rotating recorders: 60 s segments, 2 s overlap | small uploads, no lost words at the cuts |
| Voice caps fail closed when Redis is down | GPT-Live costs money per second |
| Invite token → short-lived HMAC session token | the invite link alone can't call turn APIs forever |
| Embedded Postgres (pgserver) for dev/tests | Docker isn't available on this machine |
| UI follows twincafe.com: Manrope + Instrument Serif, cream, pill buttons | consistent look with TwinCafe |

## GPT-Live findings (docs, Nov 2026)

| Topic | Finding |
|---|---|
| Session create | `POST /v1/live/sessions` with `{session, transport:{type:"webrtc", sdp}}` |
| Data channel | named `oai-events` |
| Guidance | `session.thinking.append`, `delegation_id: null` |
| Transcript completion | no turn-done event, so turns are grouped in the browser on the input→output switch |
| Delegation | `session.delegation.created` has no task text; answer with `session.commentary.append` |
| Max duration | none, so the browser and server enforce it |
| Price | $0.05/min, billed per second, 15 s billed at creation |

## Tested with real keys (2026-11-02)

Runs: `scripts/smoke.py` (text interview), `scripts/audio_cases.py` (synthetic speech from
Deepgram Aura + OpenAI TTS through Deepgram → signals → observer → planner), and live GPT-Live
sessions from the browser with synthetic audio.

| Case | Result | Change made |
|---|---|---|
| Text reply latency | 10–16 s with default reasoning | `reasoning` per task in `config.yaml`; replies ~5 s at `low` (`minimal` is ~2 s but repeats its own last question) |
| Analyze latency | 36 s → ~6 s | same |
| Interviewer re-introduced itself every turn | intro block sent on every turn | intro only on the opening; `ONGOING` block after |
| Interviewer copied the prompt's example question | example too close to the domain | neutral example; no quoting their words back |
| Tired "I don't know, that's all" flagged as deflection | would bar the topic forever | observer prompt: deflection only on explicit refusal |
| Note saved about a deflected topic | | `observer.clean` drops facts/interests mentioning it |
| `register` field silently optional | clashed with `BaseModel.register` | renamed `talk_register`, JSON alias `register` |
| Fillers ("um") | `language=multi` drops them, `en` keeps them | Deepgram language follows the subject; filler rate marked unavailable for multilingual subjects |
| Hinglish | `multi` returns Devanagari + Latin, code-switch rate works; `en` loses the Hindi | as above |
| Emotion | Deepgram Aura has no emotion control; Deepgram `sentiment` (English only, text-based) separates excited +0.88 / tired −0.22 / sad −0.61 | per-word sentiment → `sentiment` timing feature for English subjects |
| Long pause before answer | tagged at 3.5 s | — |
| Segment overlap merge | word-for-word identical to the uncut recording | — |
| Echo without headphones | the AI's question leaked into the subject's turn; diarization didn't separate the voices | echo removal matches the interviewer's text in order and in time; barge-in words kept |
| GPT-Live session create | works as documented; connect ~0.7 s | — |
| GPT-Live speaking first | the `session.input` developer message alone didn't start it in 25 s; a `session.commentary.append` nudge after `session.started` did (2.5–10 s) | both sent |
| GPT-Live with no audio frames | closed with `expired` after ~28 s | none needed (a real mic always sends frames) |
| GPT-Live hearing Hinglish | transcript exact; replies in the subject's language; ~2.4 s after they stop | — |
| GPT-Live interrupted mid-sentence | finishes the phrase (~2.6 s), backchannels "Mm.", asks a follow-up after | — |

## Still to verify

- [ ] Supabase signed-upload endpoint shape (`app/storage.py`) — needs a Supabase project
- [ ] Deepgram async callback delivery (needs a public URL; sync responses verified)
- [ ] Real human speech: pauses and fillers from TTS are weaker than real speech
- [ ] Browser VAD energy features (only measurable with a real mic)
- [ ] Voice `nira` for Hindi-first subjects
