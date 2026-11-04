"""Rough twin + probe tests (plan 7.7 / 7.8). Evaluation tools, not the product."""

from __future__ import annotations

from pydantic import BaseModel

from app import db
from app.llm import llm, prompt
from app.models import TwinSpec


class ProbeQuestion(BaseModel):
    slot: str
    item: str
    question: str


class ProbeSet(BaseModel):
    questions: list[ProbeQuestion]


class ProbeVerdict(BaseModel):
    slot: str
    item: str
    answerable: bool
    reason: str


class ProbeVerdicts(BaseModel):
    verdicts: list[ProbeVerdict]


def probe_domain(conn, twin_id: str, spec: TwinSpec, domain_id: str, items: list[str],
                 limit: int) -> list[ProbeVerdict]:
    d = next((d for d in spec.content_domains if d.id == domain_id), None)
    if d is None:
        return []
    pairs = [(i, s) for i in (items or [""]) for s in d.slot_template][:limit]
    ask = "\n".join(f"- item: '{i}', slot: {s}" for i, s in pairs)
    qs = llm().json("probe_questions", prompt("probe_generator"),
                    f"Person: {spec.subject.name}\nDomain: {d.name}\nAsk about:\n{ask}", ProbeSet)
    if not qs.questions:
        return []
    material = "\n".join(f"- {r['text']}" for r in db.all_(
        conn, "select text from notes where twin_id=%s order by created_at desc limit 150",
        (twin_id,)))
    excerpts = "\n".join(f"> {r['text'][:400]}" for r in db.all_(
        conn, "select text from corpus_items where twin_id=%s order by created_at desc limit 40",
        (twin_id,)))
    questions = "\n".join(f"- [{q.item}|{q.slot}] {q.question}" for q in qs.questions)
    out = llm().json("probe_judge", prompt("probe_judge"),
                     f"Notes:\n{material}\n\nExcerpts:\n{excerpts}\n\nQuestions:\n{questions}",
                     ProbeVerdicts)
    return out.verdicts


def qa_pairs(conn, twin_id: str, turn_ids: list[str] | None = None) -> list[dict]:
    """Each of their interview answers with the interviewer line before it (skipping
    "mm-hm"-style backchannels), oldest first. `low_energy` marks answers the observer read as
    flat, which say more about the moment than about how they talk."""
    rows = db.all_(conn, """
        select t.id::text as turn_id, coalesce(t.text_live, t.text_corpus) as answer,
          (o.output->>'lexical_engagement') = 'low' as low_energy,
          (select coalesce(q.text_live, q.text_corpus) from turns q
            where q.session_id = t.session_id and q.speaker = 'interviewer' and q.idx < t.idx
              and (coalesce(q.text_live, q.text_corpus) like '%%?%%'
                   or array_length(regexp_split_to_array(
                        trim(coalesce(q.text_live, q.text_corpus, '')), '\\s+'), 1) > 2)
            order by q.idx desc limit 1) as question
        from turns t join sessions s on s.id = t.session_id
        left join observer_outputs o on o.turn_id = t.id
        where t.twin_id = %s and t.speaker = 'subject'
          and coalesce(t.text_live, t.text_corpus, '') <> ''
          and (%s::uuid[] is null or t.id = any(%s::uuid[]))
        order by s.started_at nulls last, t.idx""", (twin_id, turn_ids, turn_ids))
    return [{**r, "low_energy": bool(r["low_energy"])} for r in rows]


MAX_RETRIEVED_PAIRS = 3


def answer(twin_id: str, question: str, exclude_turn_ids: list[str] | None = None) -> str:
    """The rough twin: LLM + retrieved corpus + notes. Real exchanges (the voice
    examples picked after each session, plus the ones closest to this question) go in as
    earlier turns of the chat, so it answers in their voice by continuing it."""
    with db.tx() as conn:
        spec_row = db.one(conn, """select spec from twin_specs where twin_id=%s and approved_at
                                   is not null order by version desc limit 1""", (twin_id,))
        spec = TwinSpec.model_validate(spec_row["spec"])
        exclude = exclude_turn_ids or []
        has_vec = db.one(conn, "select 1 from corpus_items where twin_id=%s and embedding is not "
                               "null limit 1", (twin_id,))
        if has_vec:
            vec = llm().embed([question])[0]
            rows = db.all_(conn, """select text, turn_id::text from corpus_items where twin_id=%s
                and embedding is not null and (turn_id is null or not (turn_id = any(%s::uuid[])))
                order by embedding <=> %s::vector limit 8""", (twin_id, exclude, str(vec)))
        else:
            rows = db.all_(conn, """select text, turn_id::text from corpus_items where twin_id=%s
                and (turn_id is null or not (turn_id = any(%s::uuid[])))
                order by created_at desc limit 8""", (twin_id, exclude))
        voice = db.all_(conn, """select turn_id::text, question, answer from voice_examples
            where twin_id=%s and (turn_id is null or not (turn_id = any(%s::uuid[])))
            order by created_at""", (twin_id, exclude))
        near_ids = [r["turn_id"] for r in rows if r["turn_id"]][:MAX_RETRIEVED_PAIRS]
        near = qa_pairs(conn, twin_id, near_ids) if near_ids else []
        notes = "\n".join(f"- {r['text']}" for r in db.all_(
            conn, "select text from notes where twin_id=%s and kind <> 'speech_habit' "
                  "order by created_at desc limit 60", (twin_id,)))
        habits = "\n".join(f"- {r['text']}" for r in db.all_(
            conn, "select text from notes where twin_id=%s and kind='speech_habit'", (twin_id,)))
        lengths = sorted(len(p["answer"].split()) for p in qa_pairs(conn, twin_id)
                         if not p["low_energy"])
    # Left alone, the model writes 150-word answers for someone who talks in 20-word ones, and
    # a long polished answer dilutes every habit; their real length is the biggest single fix.
    length = (f"Their answers are usually about {lengths[len(lengths) // 2]} words, rarely over "
              f"{lengths[int(len(lengths) * 0.9)]}. Answer at that length."
              if len(lengths) >= 5 else "")
    instructions = prompt("twin").format(
        subject_name=spec.subject.name, notes=notes or "(none)", length=length,
        habits=habits or "(none found yet)",
        examples="\n".join(f"> {r['text'][:500]}" for r in rows if not r["turn_id"])
        or "(none)")
    seen, messages = set(), []
    for p in [*voice, *near]:
        if p["turn_id"] in seen or not p.get("question"):
            continue
        seen.add(p["turn_id"])
        messages += [{"role": "user", "content": p["question"]},
                     {"role": "assistant", "content": p["answer"]}]
    messages.append({"role": "user", "content": question})
    return llm().text("twin", instructions, messages)
