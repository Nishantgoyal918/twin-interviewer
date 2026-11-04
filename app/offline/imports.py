"""Existing corpora: WhatsApp exports and plain text (plan 8.1 step 4)."""

from __future__ import annotations

import re

# "12/03/2024, 21:15 - Mom: text"  or  "[12/03/24, 9:15:02 PM] Mom: text"
WA_LINE = re.compile(
    r"^\[?(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]?"
    r"\s*(?:-\s*)?([^:]{1,60}):\s?(.*)$"
)
SKIP = ("<Media omitted>", "This message was deleted", "Messages and calls are end-to-end")


def parse_whatsapp(text: str, author: str) -> list[str]:
    """Returns the author's messages, joining continuation lines."""
    out: list[str] = []
    current_author: str | None = None
    for raw in text.splitlines():
        line = raw.replace("‎", "").strip()
        m = WA_LINE.match(line)
        if m:
            current_author = m.group(3).strip()
            body = m.group(4).strip()
            if current_author.lower() == author.lower() and body and not body.startswith(SKIP):
                out.append(body)
            else:
                current_author = current_author if current_author.lower() == author.lower() else None
            continue
        if line and current_author and out:
            out[-1] += "\n" + line
    return out


def parse_plain(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [p[:4000] for p in paras]
