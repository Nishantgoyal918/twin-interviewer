"""Brief compiler: creator's plain-language brief → TwinSpec (plan 7.1)."""

from __future__ import annotations

import re

from app.llm import llm, prompt
from app.models import TwinSpec
from app.settings import get_settings


def compile_brief(brief: str) -> TwinSpec:
    spec = llm().json("brief_compiler", prompt("brief_compiler"), brief, TwinSpec)
    return normalize(spec)


def normalize(spec: TwinSpec) -> TwinSpec:
    cfg = get_settings().cfg
    seen: set[str] = set()
    for d in spec.content_domains:
        d.id = re.sub(r"[^a-z0-9_]+", "_", d.id.lower()).strip("_") or "domain"
        while d.id in seen:
            d.id += "_2"
        seen.add(d.id)
        d.slot_template = [re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")
                           for s in d.slot_template if s.strip()] or ["details"]
    w = spec.register_weights
    total = w.instructing + w.storytelling + w.opinion + w.reacting + w.small_talk
    if total > 0:
        for name in ("instructing", "storytelling", "opinion", "reacting", "small_talk"):
            setattr(w, name, round(getattr(w, name) / total, 3))
    p = spec.session_prefs
    p.max_minutes = max(5, min(p.max_minutes or 30, int(cfg("session.default_max_minutes", 30))))
    p.target_minutes = max(3, min(p.target_minutes or 20, p.max_minutes))
    p.text_idle_end_hours = p.text_idle_end_hours or int(cfg("session.text_idle_end_hours", 12))
    if not spec.modalities:
        spec.modalities = ["voice", "text"]
    return spec
