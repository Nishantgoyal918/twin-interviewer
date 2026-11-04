"""Observer (plan 7.4): one LLM call per subject turn, validated against the spec."""

from __future__ import annotations

from pydantic import ValidationError

from app.llm import Timer, llm, prompt
from app.models import ObserverOutput, TwinSpec


def domains_text(spec: TwinSpec, items: dict[str, list[str]]) -> str:
    lines = []
    for d in spec.content_domains:
        known = items.get(d.id, [])
        extra = f" (open list; known items: {', '.join(known) or 'none yet'})" if d.open_list else ""
        lines.append(f"- {d.id}: {d.name}{extra}; slots: {', '.join(d.slot_template)}")
    return "\n".join(lines)


def observe(spec: TwinSpec, transcript: str, notes: str,
            items: dict[str, list[str]]) -> tuple[ObserverOutput, int]:
    """Returns (validated output, latency ms). Retries once on invalid output."""
    instructions = (prompt("observer") + "\n\n## Coverage slots\n" + domains_text(spec, items)
                    + "\n\n## Known notes\n" + (notes or "(none)"))
    last_err: Exception | None = None
    with Timer() as t:
        for _ in range(2):
            try:
                out = llm().json("observer", instructions, transcript, ObserverOutput)
                break
            except (ValidationError, ValueError) as exc:
                last_err = exc
        else:
            raise RuntimeError(f"observer failed: {last_err}")
    return clean(out, spec), t.ms


def clean(out: ObserverOutput, spec: TwinSpec) -> ObserverOutput:
    domains = {d.id: d for d in spec.content_domains}
    kept = []
    for u in out.slot_updates:
        d = domains.get(u.domain_id)
        if d is None:
            continue
        if not d.open_list:
            u.item = ""
        elif not u.item.strip():
            continue
        u.item = u.item.strip().lower()[:80]
        if u.slot != "_items" and u.slot not in d.slot_template:
            continue
        kept.append(u)
    out.slot_updates = kept
    out.topics = [t.strip().lower()[:60] for t in out.topics if t.strip()][:4]
    out.new_interests = [t.strip().lower()[:60] for t in out.new_interests if t.strip()][:4]
    if out.deflected_topic:
        out.deflected_topic = out.deflected_topic.strip().lower()[:60]
    if out.deflection and out.deflected_topic:
        # Nothing about a declined topic is kept, so it can't resurface via notes or bridges.
        topic = out.deflected_topic
        out.facts = [f for f in out.facts if topic not in f.text.lower()]
        out.new_interests = [t for t in out.new_interests if topic not in t]
    return out
