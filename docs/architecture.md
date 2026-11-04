# Architecture

Serverless-first. There is one FastAPI app (`api/index.py` → `app.main`) on Vercel, plus
managed services. The full reasoning is in plan v3, sections 6–10.

```
browser ─┬─ /api/*  (FastAPI on Vercel) ──┬─ Postgres + pgvector (Supabase)   source of truth
         │                                ├─ Redis (Upstash)                  hot state, locks, caps
         │                                ├─ QStash                           background jobs
         │                                └─ Storage (Supabase "audio")       recordings
         ├─ WebRTC ── OpenAI GPT-Live (voice; browser talks to it directly after /api/voice/connect)
         └─ PUT signed URL ── Storage (audio segments)
Deepgram ── POST /api/webhooks/deepgram (transcripts back)
```

## One turn, two lanes

1. **Fast lane:** `POST /api/turn` saves the subject's words. In text mode it also writes the
   interviewer's reply. It holds a per-session Redis lock and gives each turn the next `idx` in
   Postgres. It is idempotent on `client_turn_id`.
2. **Slow lane:** `POST /api/turn/{id}/analyze`, called by the browser in the background.
   - The observer LLM extracts facts, slots, register and deflection.
   - The text and voice features become z-score tags against the twin's and the session's
     baselines.
   - The planner (plain code) picks the next goal and writes guidance.
   - Everything is persisted, and SessionState is saved in Redis with a Postgres snapshot.
   - In voice mode the guidance goes to GPT-Live via `session.thinking.append`.

**Lock scope.** Both lanes use the same per-session lock, but the slow lane never holds it
during the observer LLM call (6–12 s), so a `/turn` is never stuck behind the thinking (it only
waits `turn.lock_wait_ms` before returning 409). `analyze` takes the lock twice, with the LLM call in between:

1. *Read* (locked): load the turn, session, spec, notes and transcript. If `analyzed_at` is
   already set, return the stored decision.
2. *Think* (unlocked): run the observer.
3. *Apply* (locked): check `analyzed_at` again, so two analyses of the same turn stay
   idempotent (the loser returns the winner's decision and its observer output is dropped).
   Reload SessionState, TwinState and baselines fresh, then build tags, plan, persist, write the
   snapshot and save SessionState.

`/turn` only reads SessionState; the slow lane is its only writer, and `last_analyzed_idx`
never moves backwards when analyses finish out of order.

Turns the browser never analyzed get picked up when the session ends (`analyze_backlog`).

## After a session (job chain)

The jobs run in this order:

1. `analyze_backlog`
2. `transcribe_session`, which sends each audio segment to Deepgram with a callback
3. The webhook calls, one per segment, fan in
4. `align_transcript`, which merges overlaps, drops echo and assigns words to turns
5. `timing_features`, which computes pause, rate, filler and code-switch signals and
   per-source baselines
6. `coverage_update`, which checks register stability, embeds new corpus and notes, and runs
   probes

The session is then marked `processed`.

Each job is a row in `jobs` (idempotency key, attempts). It is dispatched inline locally, or
through QStash to `/api/jobs/{kind}`. A daily cron re-runs stuck jobs.

## Code map

| Area | Files |
|---|---|
| HTTP | `app/routers/{creator,subject,machine}.py`, `app/main.py` |
| Sessions and turns | `app/services/{sessions,turns,context}.py` |
| AI agents | `app/{brief,observer,conversation,twin}.py`, `prompts/` |
| Planner | `app/planner/{planner,policy,coverage}.py` |
| Signals | `app/signals/{text_signals,timing_features,baseline,tags}.py` |
| Offline | `app/offline/{pipeline,deepgram,align,style,imports}.py` |
| Infra | `app/{db,queue,storage,llm,settings}.py`, `app/cache/redis.py`, `app/security/` |
| UI | `public/` (plain HTML + JS modules, one stylesheet) |

## Failure behaviour

- **Redis down:** turns still work. State is rebuilt from the latest snapshot. Locks and
  idempotency fall back to the unique `(session_id, client_turn_id)` constraint. Voice
  sessions refuse to start (caps fail closed).
- **LLM error on analyze:** the turn stays unanalyzed and the backlog job retries it.
- **Upload failure:** the browser keeps the segment in IndexedDB and retries.
