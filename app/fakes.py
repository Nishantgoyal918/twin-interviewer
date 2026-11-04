"""Canned LLM output for LLM_BACKEND=fake (local dev without an OpenAI key, and tests)."""

from __future__ import annotations

import re
from typing import Any


def fake_spec(brief: str) -> dict[str, Any]:
    m = re.search(r"(?:twin of|of)\s+(?:my\s+)?(\w+)", brief, re.I)
    name = (m.group(1) if m else "Subject").capitalize()
    return {
        "subject": {"name": name, "relation_to_creator": None, "languages": ["en"],
                    "code_switching": False},
        "audience": "family",
        "purpose": brief[:200],
        "interviewer_stance": "curious younger relative",
        "motivation_archetypes": ["legacy", "being_heard"],
        "content_domains": [
            {"id": "life_story", "name": "Life story", "priority": "high", "open_list": False,
             "slot_template": ["childhood", "work", "turning_points"]},
            {"id": "recipes", "name": "Recipes", "priority": "med", "open_list": True,
             "slot_template": ["steps", "quantities", "timing_cues", "substitutions"]},
        ],
        "register_weights": {"instructing": 0.25, "storytelling": 0.3, "opinion": 0.2,
                             "reacting": 0.15, "small_talk": 0.1},
        "emotional_range_weight": 0.5,
        "modalities": ["voice", "text"],
        "existing_corpus": [],
        "sensitivities": [],
        "creator_seeds": [],
        "session_prefs": {"target_minutes": 20, "max_minutes": 30, "text_idle_end_hours": 12},
    }


def fake_observer(text: str) -> dict[str, Any]:
    last = text.strip().splitlines()[-1] if text.strip() else ""
    words = re.findall(r"\w+", last.lower())
    register = "instructing" if {"first", "then", "add"} & set(words) else (
        "storytelling" if {"when", "remember", "was"} & set(words) else "small_talk")
    deflect = bool({"rather", "skip", "don't"} & set(words)) and "talk" in words
    engagement = "high" if len(words) > 40 else "medium" if len(words) > 8 else "low"
    return {
        "facts": [{"kind": "fact", "text": last[:140]}] if len(words) > 5 else [],
        "topics": [w for w in words if len(w) > 6][:3],
        "register": register,
        "new_interests": [],
        "deflection": deflect,
        "deflected_topic": None,
        "lexical_engagement": engagement,
        "slot_updates": [],
    }


def fake_json(task: str, input: str) -> dict[str, Any]:
    if task == "brief_compiler":
        return fake_spec(input)
    if task == "observer":
        return fake_observer(input)
    if task == "probe_questions":
        return {"questions": []}
    if task == "speech_habits":
        return {"items": []}
    if task == "sim_persona":
        return {"effect": 0, "reason": "fake"}
    if task == "probe_judge":
        return {"verdicts": []}
    raise KeyError(f"no fake for task {task}")
