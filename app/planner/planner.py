"""Planner step for one analyzed subject turn. Pure function of its inputs (easy to replay)."""

from __future__ import annotations

from app.models import Decision, Goal, ObserverOutput, SessionState, Tag, TwinSpec
from app.planner import coverage, policy
from app.planner.coverage import TwinState

# Breadth over depth. They run with their own thread for this many engaged turns before the
# interviewer bridges to a new area (fewer in the second half of the call), and a goal that
# hasn't come up after this long is dropped for the session. Patience is in seconds, not
# turns: one spoken answer often arrives as several fragments.
THREAD_TURNS, THREAD_TURNS_LATE = 3, 2
GOAL_PATIENCE_S, GOAL_PATIENCE_LATE_S = 90, 60
# How to open a new area: a process question ("what do you handle day to day?") as the first
# step into a topic dulls people; a small concrete story or favourite draws them in.
OPENER = ("Open it with an easy, concrete question (a favourite, a time when, a proudest "
          "moment), not a how-does-it-work question; the details can follow.")


def plan(spec: TwinSpec, state: SessionState, ts: TwinState, obs: ObserverOutput,
         tags: list[Tag], word_count: int, turn_idx: int) -> Decision:
    """Updates `state` in place and returns the decision (guidance for the next turn)."""
    per_turn = policy.turn_state(obs, tags, word_count, state.subject_word_counts)
    state.engagement_trend = (state.engagement_trend + [per_turn])[-10:]
    state.subject_word_counts = (state.subject_word_counts + [word_count])[-10:]
    if per_turn == "engaged":
        for t in obs.topics[:2]:
            if t not in state.bridge_sources:
                state.bridge_sources = (state.bridge_sources + [t])[-5:]

    budget = state.session_budget
    engagement = policy.trend_state(state.engagement_trend, budget.elapsed_s,
                                    budget.target_minutes)
    over_max = budget.elapsed_s >= budget.max_duration_s

    dropped: str | None = None
    goal: Goal | None = state.current_goal
    if engagement == "avoidant" or obs.deflection:
        dropped = obs.deflected_topic or (obs.topics[0] if obs.topics else None)
        if dropped:
            ts.do_not_raise.add(dropped)
        engagement = "avoidant"

    late = budget.elapsed_s >= 0.5 * budget.target_minutes * 60
    firm = False
    if engagement != "engaged":
        state.engaged_run = 0

    if engagement == "tired" or over_max:
        goal = Goal(id="wrap_up", kind="wrap_up", set_at_turn=turn_idx)
        state.wrap_up = True
        policy_name = "wrap_up"
        guidance = ("Wrap up at the next natural moment, on a high note. Thank them warmly, "
                    "briefly. Don't start a new topic.")
    elif engagement == "engaged":
        # Let them run, but not for the whole call: a happy person who is never steered
        # gives a twin that knows one topic. The warm moment is the easiest time to switch.
        state.engaged_run += 1
        nxt = _next_goal(spec, ts, state, turn_idx, exclude_domain=_domain(goal))
        if nxt is not None and state.engaged_run >= (THREAD_TURNS_LATE if late else THREAD_TURNS):
            policy_name, goal, state.engaged_run, firm = "bridge", nxt, 0, True
            guidance = ("They're enjoying this, which makes it the best moment to open something "
                        "new. When this thought ends, react, then bridge from what they just said "
                        f"to a new topic: {coverage.describe(goal, spec)}. {OPENER}")
        else:
            policy_name = "let_them_run"
            guidance = ("They're engaged. Don't redirect yet. Follow their thread with short "
                        "follow-ups about what they just said.")
    elif engagement == "avoidant":
        policy_name = "drop_topic"
        goal = _next_goal(spec, ts, state, turn_idx, exclude_domain=_domain(state.current_goal))
        what = f"'{dropped}'" if dropped else "that topic"
        guidance = (f"They're avoiding {what}. Drop it and never bring it back. "
                    f"Gently move on" + _goal_text(goal, spec, ts, state))
    elif engagement == "flat":
        policy_name = "change_kind"
        last_kind = state.current_goal.kind if state.current_goal else None
        goal = (coverage.choice_goal(spec, ts) if last_kind != "choice"
                else coverage.register_goal(spec, ts) or coverage.choice_goal(spec, ts))
        goal.set_at_turn = turn_idx
        guidance = ("Energy is low. Change the kind of question, not just the topic: "
                    + coverage.describe(goal, spec) + ".")
    else:
        policy_name = "continue"
        waited = budget.elapsed_s - state.goal_since_s
        stale = goal is not None and waited >= (GOAL_PATIENCE_LATE_S if late else GOAL_PATIENCE_S)
        if stale:   # it never came up: give up on it for this session, try another area
            state.skipped_goals = (state.skipped_goals + [goal.id])[-20:]
        if goal is None or stale or _goal_done(goal, ts) or goal.kind in ("choice", "register"):
            goal = _next_goal(spec, ts, state, turn_idx,
                              exclude_domain=_domain(goal) if stale else None)
        unseen = _untouched_domains(spec, ts)
        if late and goal is not None and goal.kind == "content_slot" and goal.domain_id in unseen:
            # Second half, and the goal's whole area hasn't come up: ask it, don't hint at it.
            names = ", ".join(list(unseen.values())[:3])
            firm = True
            guidance = (f"Time is passing and some topics haven't come up yet ({names}). "
                        "With your next question, move on: "
                        f"{coverage.describe(goal, spec)}. Bridge from what they just said; "
                        f"don't cut off a story. {OPENER}")
        else:
            guidance = "Carry on naturally. Soft goal" + _goal_text(goal, spec, ts, state)

    if goal is not None and (state.current_goal is None or goal.id != state.current_goal.id):
        state.goal_since_s = budget.elapsed_s
        if goal.domain_id and goal.domain_id not in state.domains_visited:
            state.domains_visited = state.domains_visited + [goal.domain_id]
    state.current_goal = goal
    state.guidance = guidance
    return Decision(engagement=engagement, policy=policy_name, goal=goal, guidance=guidance,
                    wrap_up=state.wrap_up, dropped_topic=dropped,
                    tags=[t.model_dump() for t in tags], firm=firm)


def _domain(goal: Goal | None) -> str | None:
    return goal.domain_id if goal else None


def _visited(ts: TwinState, state: SessionState) -> set[str]:
    """Domains this session already turned to, or that earlier talk already touched."""
    return set(state.domains_visited) | _learned(ts)


def _learned(ts: TwinState) -> set[str]:
    """Domains with at least one thing really learned (a passing mention only makes a slot
    'partial': "I head into work stuff" doesn't mean work was covered)."""
    return ({d for (d, _i, _s), st in ts.slots.items() if st == "done"}
            | {d for d, items in ts.items.items() if items})


def _untouched_domains(spec: TwinSpec, ts: TwinState) -> dict[str, str]:
    """Domains nothing has been learned about yet (id → name), most important first."""
    talked = _learned(ts)
    return {d.id: d.name for d in sorted(spec.content_domains,
                                         key=lambda d: coverage.PRIORITY[d.priority])
            if d.id not in talked and not coverage.blocked(d.name, ts.do_not_raise)}


def _goal_done(goal: Goal, ts: TwinState) -> bool:
    if goal.kind != "content_slot" or goal.slot == "_items":
        return goal.slot == "_items" and bool(ts.items.get(goal.domain_id or ""))
    return ts.slots.get((goal.domain_id or "", goal.item or "", goal.slot or "")) == "done"


def _next_goal(spec: TwinSpec, ts: TwinState, state: SessionState, turn_idx: int = 0,
               exclude_domain: str | None = None) -> Goal | None:
    """Breadth first: a domain nobody has touched yet beats finishing one that's under way, so
    a short call samples everything the twin needs instead of drilling into one topic."""
    seen = _visited(ts, state)
    goals = [g for g in coverage.content_goals(spec, ts, exclude_domain)
             if g.id not in state.skipped_goals]
    fresh = [g for g in goals if g.domain_id not in seen]
    partial = [g for g in goals
               if ts.slots.get((g.domain_id or "", g.item or "", g.slot or "")) == "partial"]
    goal = (fresh or partial or goals or [None])[0]
    if goal is None:
        goal = coverage.seed_goal(ts) or coverage.register_goal(spec, ts)
    if goal is not None:
        goal.set_at_turn = turn_idx
    return goal


def _goal_text(goal: Goal | None, spec: TwinSpec, ts: TwinState, state: SessionState) -> str:
    if goal is None:
        return ". Nothing specific to cover; follow their lead."
    text = f": {coverage.describe(goal, spec)}."
    bridge = coverage.best_bridge(ts, state.bridge_sources)
    if bridge and goal.kind == "content_slot":
        goal.bridge = bridge
        text += f" If you need a way in, bridge from '{bridge}', which they enjoyed."
    return text + " Only when it fits; don't interrupt a story."
