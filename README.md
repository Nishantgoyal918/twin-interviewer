# Twin Interviewer

An AI interviewer that talks with a person, by voice or text, to build a digital twin of them.
It covers what they know, such as recipes or life stories, and how they talk, including their
words, rhythm and code-switching. It reads engagement from text and timing signals and steers
the conversation.

- Live: https://twin-interviewer-pi.vercel.app (creator console at `/creator`)
- Design pages (served by the app, files in `public/`):
  - `/design-doc`: the AI design, with diagrams and the rules each part follows
  - `/design-doc-v2`: the same, plus a proposed on-device video feed for reading emotion
  - `/design-doc/full`: the whole system (data model, hosting, every flow)
- Build log: [`PROGRESS.md`](PROGRESS.md)
- Docs: [`docs/`](docs/), covering architecture, setup, API, data model, AI agents, decisions and the
  [interview simulator](docs/simulator.md)

## Run it locally (no accounts needed)

```bash
uv sync --python 3.12
uv run python scripts/dev.py          # http://localhost:8000/creator
uv run pytest                         # 57 tests, embedded Postgres, no network
uv run python scripts/smoke.py        # real text interview (needs OPENAI_API_KEY, dev server running)
uv run python scripts/audio_cases.py  # synthetic voice cases (needs both keys)
uv run ruff check .
```

Locally, the app runs with these stand-ins:

- **Postgres:** an embedded Postgres with pgvector.
- **Redis:** an in-memory stand-in.
- **Jobs:** run inline.
- **File storage:** files in `.data/`.
- **Login:** a dev login.
- **LLM:** a fake one.

Put `OPENAI_API_KEY` in `.env` to use the real models. Voice also needs that key, and
transcription needs `DEEPGRAM_API_KEY`. See [docs/setup.md](docs/setup.md).

## Layout

```
api/index.py          Vercel entry (FastAPI)
app/                  backend: routers, services, planner, signals, offline pipeline
prompts/              LLM prompts (markdown)
public/               static pages: creator console, subject chat + voice, design pages
devtools/             interview simulator page (/dev/sim, dev only or behind SIM_KEY)
personas/             simulated people for the simulator
migrations/           SQL schema (applied in order by scripts/migrate.py)
scripts/              dev server, migrate, simulator scoring, design page build
tests/                unit + end-to-end flow tests
config.yaml           tunables (models, caps, thresholds)
```
