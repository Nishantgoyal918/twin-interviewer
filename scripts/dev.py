"""Run the app locally with no cloud accounts: embedded Postgres (pgserver + pgvector),
in-memory cache, inline jobs, local file storage, dev auth. LLM is fake unless
OPENAI_API_KEY is set in .env.

    uv run python scripts/dev.py            # http://localhost:8000
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(port: int = 8000) -> None:
    import pgserver
    import uvicorn

    from app.settings import _load_dotenv

    _load_dotenv()
    if not os.environ.get("DATABASE_URL"):
        data = ROOT / ".data" / "pg"
        data.mkdir(parents=True, exist_ok=True)
        os.environ["DATABASE_URL"] = pgserver.get_server(str(data), cleanup_mode=None).get_uri()
    defaults = {"CACHE_BACKEND": "memory", "QUEUE_MODE": "inline", "STORAGE_BACKEND": "local",
                "DEV_AUTH": "1", "APP_BASE_URL": f"http://localhost:{port}",
                "LLM_BACKEND": "openai" if os.environ.get("OPENAI_API_KEY") else "fake"}
    for k, v in defaults.items():
        os.environ.setdefault(k, v)

    from scripts.migrate import migrate

    migrate(os.environ["DATABASE_URL"])
    print(f"LLM={os.environ['LLM_BACKEND']}  creator page: http://localhost:{port}/creator")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
