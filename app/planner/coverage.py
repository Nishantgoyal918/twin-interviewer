"""Choosing the next soft goal from coverage gaps (plan 7.6 step 4)."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import REGISTERS, Goal, TwinSpec

PRIORITY = {"high": 0, "med": 1, "low": 2}


@dataclass
class TwinState:
    """Durable cross-session state, loaded from its own tables (plan 7.3)."""

    slots: dict[tuple[str, str, str], str] = field(default_factory=dict)   # (domain,item,slot)→status
    items: dict[str, list[str]] = field(default_factory=dict)               # open-list items seen
    interests: dict[str, float] = field(default_factory=dict)
    do_not_raise: set[str] = field(default_factory=set)
    register_words: dict[str, int] = field(default_factory=dict)
    seeds: list[str] = field(default_factory=list)                          # creator seeds + twin gaps


def blocked(text: str, dnr: set[str]) -> bool:
    low = text.lower()
    return any(t.lower() in low or low in t.lower() for t in dnr if t)


def best_bridge(ts: TwinState, bridge_sources: list[str]) -> str | None:
    for b in reversed(bridge_sources):
        if not blocked(b, ts.do_not_raise):
            return b
    ranked = sorted(ts.interests.items(), key=lambda kv: -kv[1])
    for topic, score in ranked:
        if score >= 0.6 and not blocked(topic, ts.do_not_raise):
            return topic
    return None


def content_goals(spec: TwinSpec, ts: TwinState, exclude_domain: str | None = None) -> list[Goal]:
    out: list[Goal] = []
    for d in sorted(spec.content_domains, key=lambda d: PRIORITY[d.priority]):
        if d.id == exclude_domain or blocked(d.name, ts.do_not_raise):
            continue
        items = ts.items.get(d.id, []) if d.open_list else [""]
        if d.open_list and not items:
            out.append(Goal(id=f"{d.id}::items", kind="content_slot", domain_id=d.id,
                            item="", slot="_items"))
            continue
        for item in items:
            if item and blocked(item, ts.do_not_raise):
                continue
            for slot in d.slot_template:
                status = ts.slots.get((d.id, item, slot), "empty")
                if status != "done":
                    out.append(Goal(id=f"{d.id}:{item}:{slot}", kind="content_slot",
                                    domain_id=d.id, item=item, slot=slot))
    # partially filled slots first within the same priority: finish what's started
    return out


def register_goal(spec: TwinSpec, ts: TwinState) -> Goal | None:
    weights = spec.register_weights.model_dump()
    total = sum(ts.register_words.values()) or 1
    gaps = sorted(
        ((weights[r] - ts.register_words.get(r, 0) / total, r) for r in REGISTERS),
        reverse=True,
    )
    gap, reg = gaps[0]
    if gap <= 0.05:
        return None
    return Goal(id=f"register:{reg}", kind="register", register=reg)


def seed_goal(ts: TwinState) -> Goal | None:
    for s in ts.seeds:
        if not blocked(s, ts.do_not_raise):
            return Goal(id=f"seed:{s[:40]}", kind="seed", item=s)
    return None


def choice_goal(spec: TwinSpec, ts: TwinState) -> Goal:
    topics = [t for t, _ in sorted(ts.interests.items(), key=lambda kv: -kv[1])
              if not blocked(t, ts.do_not_raise)][:1]
    topics += [d.name for d in spec.content_domains if not blocked(d.name, ts.do_not_raise)]
    pair = list(dict.fromkeys(topics))[:2]
    return Goal(id="choice:" + "|".join(pair), kind="choice", item=" or ".join(pair))


def describe(goal: Goal, spec: TwinSpec) -> str:
    domains = {d.id: d.name for d in spec.content_domains}
    if goal.kind == "content_slot":
        dname = domains.get(goal.domain_id or "", goal.domain_id)
        if goal.slot == "_items":
            return f"get them to name a few things under '{dname}'"
        what = goal.slot.replace("_", " ") if goal.slot else "details"
        about = f"'{goal.item}'" if goal.item else f"'{dname}'"
        return f"learn the {what} for {about}"
    if goal.kind == "register":
        return {
            "instructing": "invite them to explain how to do something they know well",
            "storytelling": "invite a specific story (a time when…)",
            "opinion": "ask what they think about something they care about",
            "reacting": "share a small surprising detail and let them react",
            "small_talk": "keep it light for a moment: everyday small talk",
        }.get(goal.register or "", "vary the kind of question")
    if goal.kind == "seed":
        return f"if it fits, ask about '{goal.item}'"
    if goal.kind == "choice":
        return f"offer them a choice of what to talk about next: {goal.item}"
    return "wrap up at the next natural moment, on a high note, and thank them"
