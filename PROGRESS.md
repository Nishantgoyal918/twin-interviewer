# Progress

Living log of the build. Newest entries at the top of each section.
Plan: `../twin-interviewer-plan.md` (v3). Design page: `../twin-interviewer-eli5.html`.
Docs: `docs/`.

## Status by phase

| Phase | State | Notes |
|---|---|---|
| 0 Scaffolding, infra | done locally | Deploy and ASR benchmark need keys |
| 1 Text MVP | done locally | Creator → spec → invite → chat → analyze → end, tested end to end on fake LLM |
| 2 Coverage | done locally | Slots, registers, interests, never-raise, style stability, probes |
| 3 Engagement | done locally | Text and timing features, baselines, tags, planner policies |
| 4 Voice | works with real keys | GPT-Live live-tested (greeting, Hinglish, interruption); Deepgram + alignment tested on synthetic speech |
| 5 Twin + discriminator | partial | Twin answer + probes done; A/B discriminator script not written |
| 6 Generalize + worker | not started | |

## Deployed (2026-11-02)

- Live: https://twin-interviewer-pi.vercel.app (creator: `/creator`, design page: `/design-doc`)
- Vercel project `twin-interviewer` (Hobby), functions in Mumbai (`bom1`). Supabase (free,
  Mumbai: Postgres + pgvector, auth, private `audio` bucket), Upstash Redis (free, Mumbai,
  auto-upgrade off) and QStash (free, Frankfurt) were added as Vercel Marketplace
  integrations; their keys live in the project's env. Migrations 0001–0002 applied.
- Deploy: `npx vercel deploy --prod` from `twin-interviewer/`. Rebuild the design page first
  with `uv run python scripts/build_design_doc.py`.
- Vercel runs `app/main.py` with its FastAPI preset; don't add an `/api` rewrite (it hides the
  real path from the app).
- Spend: global $5/day breaker plus per-twin limits (config.yaml `caps`).

## Blocked on you (keys / accounts)

- ~~`OPENAI_API_KEY`, `DEEPGRAM_API_KEY`~~: added 2026-11-02.
- Deploying needs these accounts and keys:
  - a Supabase project: URL, anon key, service-role key, pooled `DATABASE_URL`
  - Upstash Redis and QStash tokens
  - a Vercel project

  Local dev doesn't need any of them: it uses embedded Postgres and in-memory stand-ins.

## Not done yet

- Scripts:
  - `asr_benchmark.py`
  - `replay_session.py`
  - `validate_signals.py` (kappa vs labels)
  - `twin_ab.py` (discriminator)
  - `dev_seed.py`
- Optional long-running worker.
- The checks listed in `docs/decisions.md`.

## Interview simulator: status (2026-11-02)

Built and run (see `docs/simulator.md` → Results):
- the dev-only simulator `/dev/sim`, with stereo recordings and scorecards
- 5 personas: Sunita, Rohan, Ramesh, plus Kavya ("like") and Venkat ("correct?") for speech habits
- turn-quality and speech-habit scoring

Next:
Round two confirmed the solid-flat wrap-up, the observer rules and the job retries. Next:
1. Deepgram keyterms from the live transcript (names are misheard).
2. First-session excitement reads as "uncertain" (no personal baseline yet).
3. Twins still use very frequent fillers at about half the real rate (was a third); real
   exchanges + their usual length helped (see `docs/simulator.md` → Twin voice). Venkat's
   twin over-uses his opener "what to say".
5. The call never hangs up after the goodbye: a talkative person gets minutes of "Thank you,
   Uncle" back and forth (found by the reactive Venkat run). The interviewer also copies his
   "Correct." in those goodbyes.
4. A re-run to check the "own words" reaction rule and the session-lock fix (done:
   analysis no longer holds the lock during the LLM call; 39 tests).

## Log

### 2026-11-02 (production runs, coverage, recordings)
- Simulator runs on the deployed app behind `SIM_KEY` (off again afterwards); runs live in
  storage under `sim/`. Simulated twins sit in the shared space, labelled "simulated".
- Production Kavya and Venkat: energy ~8.8/10 but only 3/5 brief topics. Found:
  - the interviewer said goodbye on its own, then a quiet cue or coach tip restarted the call
  - the planner drilled one topic, and engaged people were never steered
  - interviewer copies Venkat's "correct" (open)
- Fixed: own goodbye counts as wrapping, no tips after goodbye, a rule against early goodbyes;
  the planner goes breadth first, bridges after 3 engaged turns, drops goals after 90 s
  (60 s late), and presses for untouched areas in the second half with firm, repeated tips
  that open with a story question; fragments never read as flat; headline scorecard row
  for brief topics covered.
- Local Kavya after the fix: the interviewer moved to trekking, Mango, work and Bengaluru
  (before: bakeries only). Coverage still scored 1/5: she stalled on a work process question,
  hence the story-opener rule (not yet re-run).
- Whole-call recording (both voices, stereo) on every voice session, with a player on the
  session page; simulated runs play their simulator recording.

### 2026-11-02 (simulator)
- Ran 7 full 8-minute simulated interviews. Fixed:
  - dead air (quiet rescue + prompt rule)
  - planner lag after warming up
  - passing moods saved as facts
  - early wrap-up on short warm replies
  - rest logged as deflection
  - after-call job failing on a busy lock
- Added: speech-habit capture (`speech_habit` notes, twin-only), the interviewer reacts
  before asking, turn-quality scoring (talk share is info only), Kavya and Venkat personas.
- The design page was restyled to the TwinCafe theme, with v4 changes and simulator results.
- 37 tests pass.
- Round two of all 5 confirmed the fixes (see `docs/simulator.md`). The session-lock fix
  landed from a side session; 39 tests pass.
- Twin voice: habit extraction also picks real question → answer pairs (`voice_examples`,
  migration 0002); the twin gets them as earlier chat turns, plus a line with their usual
  answer length. Habit rate: Kavya 3.7 → 6.8, Venkat 5.5 → 8.8 per 100 words. The scorer
  now measures style on 8 open questions and reports answer length. 43 tests pass.
- Reactive personas: mood is earned turn by turn (a judge playing the persona scores each
  interviewer turn −2…+2; energy 0–10 sets the mood; fatigue after stamina). Scripted moods
  stay behind `?scripted=1`. Scorecard has "Engagement the interviewer earned". First run
  (Venkat): bored 2 → 10 within two minutes, 84% of turns engaged; a feelings question was
  the worst move; found the endless goodbye. 46 tests pass.

### 2026-11-02 (real keys)
- Ran a real text interview (`scripts/smoke.py`), a synthetic voice suite
  (`scripts/audio_cases.py`) and live GPT-Live sessions. Results and fixes are in
  `docs/decisions.md` → "Tested with real keys". Main fixes:
  - latency: per-task reasoning effort
  - the interviewer re-introducing itself
  - false deflections
  - language-aware Deepgram settings with sentiment
  - echo removal for barge-in
  - the GPT-Live greeting
- 35 tests pass.

### 2026-11-02
- UI restyled to match twincafe.com:
  - light only, cream background
  - Manrope body, Instrument Serif headings
  - near-black pill buttons and pill tabs
  - orange→pink→indigo gradient on one headline word
- Checked desktop and phone widths. Fixed `[hidden]` being overridden by the button
  `display` rule, and buttons wrapping on mobile.
- Smoke-tested in the browser with `scripts/dev.py`: create twin → approve → invite →
  consent → chat → coverage. Fixed the invite table not updating after creating a link.
- 30 tests pass (`uv run pytest`) and ruff is clean (line length raised to 110).
  - Removed the `pgcrypto` extension, since `gen_random_uuid()` is built into Postgres 13+.
  - Coverage includes: two-lane turns, idempotency across a Redis wipe, gap-free `idx` after
    state loss, deflection → never-raise, slot updates, backlog on end, resume, idle sweep,
    voice segments → simulated Deepgram → alignment, fail-closed voice caps, delegation
    recall, WhatsApp import, and the twin answer.
- Docker never started, so dev and tests use embedded Postgres (pgserver, pgvector 0.6.2).
- Wrote `docs/` (setup, architecture, api, ai-agents, data-model, decisions) and `README.md`.
- Built the whole backend, the static UI and the offline pipeline (see `docs/architecture.md`).
- Read the GPT-Live docs and settled the 5.1 [VERIFY] items. Plan 7.10 updated.
- Design page: added "The AI team" section.
