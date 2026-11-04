# Data model

Postgres is the source of truth. Redis only caches. Migrations in `migrations/` are applied in
order, once each, by `scripts/migrate.py`:

- `0001_init.sql`: everything below, unless noted
- `0002_voice_examples.sql`: `voice_examples`
- `0003_simulated_twins.sql`: `twins.simulated`

## Tables

| Group | Tables |
|---|---|
| Who | `creators`, `twins` (`simulated` = made by the simulator; shown with a label), `twin_specs` (versioned, `approved_at`), `subject_invites` (hashed tokens) |
| Conversations | `sessions` (status, clock `t0`), `turns` (`idx` unique per session, `client_turn_id` unique, text, timings, `features`, `words`) |
| Per-turn analysis | `observer_outputs`, `signals` (tag, z, confidence, source), `planner_snapshots` (SessionState + decision) |
| Long-term twin state | `notes` (+embedding), `coverage_slots`, `register_coverage`, `interests`, `do_not_raise`, `baselines` (Welford stats per source) |
| Corpus | `corpus_items` (subject's words + embedding), `imports`, `voice_examples` (real question → answer pairs that show how they talk; the twin gets them as earlier chat turns) |
| Audio | `audio_assets`: `kind='segment'` = their microphone in overlapping 60 s chunks, for Deepgram; `kind='call'` = the whole call as heard (interviewer left, them right) in 10-minute parts, for listening back on the session page |
| Ops / eval | `jobs`, `labels`, `eval_runs` |

## Redis keys

All keys are prefixed `ti:`.

| Key | What | TTL |
|---|---|---|
| `state:{sid}` | SessionState JSON | hours |
| `recent:{sid}` | recent transcript text | hours |
| `lock:{sid}` | per-session turn lock (owner token) | 90 s |
| `idem:{sid}:{cid}` | cached `/turn` response | short |
| `spec:{twin}` | active spec | minutes |
| `rl:*`, `spend:{day}` | rate limits, voice seconds, daily spend | day |

## Clocks

Every time is milliseconds on the session clock. `t0` is when the mic or chat started.
Audio segments are 60 s long with 2 s overlap. Deepgram word times get each segment's
`start_ms` added, and alignment cuts at the overlap midpoint.
