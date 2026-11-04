"""Vercel entry point: exposes the FastAPI app.

vercel.json rewrites /api/(.*) to this function, and the function then sees its own path
(/api/index). The rewrite passes the original path as `__path`; put it back before routing."""

import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app as fastapi_app  # noqa: E402

PARAM = "__path"


async def app(scope, receive, send):
    if scope["type"] == "http":
        query = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        original = next((v for k, v in query if k == PARAM), None)
        if original:
            rest = urlencode([(k, v) for k, v in query if k != PARAM])
            scope = {**scope, "path": original, "raw_path": original.encode(),
                     "query_string": rest.encode("latin-1")}
    await fastapi_app(scope, receive, send)
