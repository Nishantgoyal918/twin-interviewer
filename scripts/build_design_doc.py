"""Copies the design page (../twin-interviewer-eli5.html, also published as a claude.ai artifact)
into public/design-doc-full.html, served at /design-doc/full. The artifact file has no page skeleton
(the artifact host adds one), so this adds the doctype, head metas and the host's small reset.
Usage: uv run python scripts/build_design_doc.py"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT.parent / "twin-interviewer-eli5.html"
TARGET = ROOT / "public" / "design-doc-full.html"

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
:root { color-scheme: light; padding-top: env(safe-area-inset-top, 0px);
        padding-bottom: env(safe-area-inset-bottom, 0px); }
body { margin: 0; font: 14px system-ui, -apple-system, "Segoe UI", sans-serif; }
img { max-width: 100%; }
[hidden] { display: none !important; }
</style>
"""


def build() -> Path:
    page = SOURCE.read_text(encoding="utf-8")
    TARGET.write_text(f"{HEAD}{page}\n</html>\n", encoding="utf-8")
    return TARGET


if __name__ == "__main__":
    print(f"wrote {build().relative_to(ROOT)}")
