# Twin Interviewer

An AI interviewer that talks with a person, by voice or text, to build a digital twin of them.
It covers what they know, such as recipes or life stories, and how they talk, including their
words, rhythm and code-switching. It reads engagement from text and timing signals and steers
the conversation.

- Design: [`../twin-interviewer-plan.md`](../twin-interviewer-plan.md) (v3)
- Explainer page: [`../twin-interviewer-eli5.html`](../twin-interviewer-eli5.html)
- Build log: [`PROGRESS.md`](PROGRESS.md)
- Docs: [`docs/`](docs/), covering architecture, setup, API, data model, AI agents and decisions

## Run it locally (no accounts needed)

```bash
uv sync --python 3.12
uv run python scripts/dev.py          # http://localhost:8000/creator
uv run pytest                         # 35 tests, embedded Postgres, no network
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
public/               static pages: creator console, subject chat + voice
migrations/           SQL schema
scripts/              dev server, migrate
tests/                unit + end-to-end flow tests
config.yaml           tunables (models, caps, thresholds)
```
