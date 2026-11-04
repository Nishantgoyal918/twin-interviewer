# Setup

## Local (nothing to sign up for)

```bash
uv sync --python 3.12
uv run python scripts/dev.py      # starts embedded Postgres + app on :8000
```

Open `http://localhost:8000/creator`, describe a twin, approve the spec, and create an invite.
Then open the invite link to chat as the subject.

Local stand-ins are set by env switches in `.env` (see `.env.example`):

| Switch | Local | Production |
|---|---|---|
| `CACHE_BACKEND` | `memory` | `upstash` |
| `QUEUE_MODE` | `inline` | `qstash` |
| `STORAGE_BACKEND` | `local` (`.data/storage`) | `supabase` |
| `LLM_BACKEND` | `fake`, or `openai` if a key is set | `openai` |
| `DEV_AUTH` | `1` (`X-Dev-Creator` header) | empty (Supabase magic link) |

Leave `DATABASE_URL` empty locally. `scripts/dev.py` then starts Postgres 16 with pgvector
under `.data/pg`.

## Keys and accounts for real use

| What | Needed for | Where |
|---|---|---|
| `OPENAI_API_KEY` | brief compiler, text interviewer, observer, twin, embeddings, GPT-Live voice | platform.openai.com |
| `DEEPGRAM_API_KEY` | offline transcription of voice sessions | console.deepgram.com |
| Supabase: URL, anon key, service-role key, pooled `DATABASE_URL` (port 6543) | DB, login, audio bucket `audio` (private) | supabase.com |
| Upstash Redis REST URL + token | session state, locks, caps | upstash.com |
| Upstash QStash token + two signing keys | background jobs | upstash.com |
| Vercel project | hosting, daily cron | vercel.com |
| `INVITE_TOKEN_SECRET`, `DEEPGRAM_WEBHOOK_SECRET`, `CRON_SECRET` | ours: random strings | `python -c "import secrets;print(secrets.token_urlsafe(32))"` |

With only `OPENAI_API_KEY` added locally, text interviews run on real models.
Voice also works locally, because the browser connects to OpenAI directly.
Recordings are uploaded, but they won't be transcribed until `DEEPGRAM_API_KEY` is set.
Deepgram calls back to `APP_BASE_URL`, so it must be reachable from the internet, for example
through a tunnel.

## Deploy

1. Create a Supabase project and run `uv run python scripts/migrate.py` with
   `DATABASE_URL` set to its direct connection string.
2. Create a private Storage bucket named `audio`.
3. Set every variable from `.env.example` in Vercel. Leave the local-dev switches empty and set
   `APP_BASE_URL` to the deployment URL.
4. Run `vercel deploy`. The cron job in `vercel.json` (`/api/cron/daily`) keeps Supabase awake
   and sweeps idle sessions.

## Tests

```bash
uv run pytest        # embedded Postgres under .data/pg-test, fake LLM, no network
uv run ruff check .
```
