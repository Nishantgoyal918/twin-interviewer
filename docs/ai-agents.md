# The AI team

Four LLM roles and one piece of plain code. Models are set in `config.yaml` → `models`, and
prompts live in `prompts/`.

| Role | When | Input → output | Code |
|---|---|---|---|
| **Brief compiler** | creator writes a brief | free text → `TwinSpec` (structured output) | `app/brief.py`, `prompts/brief_compiler.md` |
| **Conversationalist** | every turn | spec + notes + guidance → next question. Text: `gpt-5-mini`. Voice: GPT-Live speaking live | `app/conversation.py`, `prompts/conversationalist_rules.md` |
| **Observer** | after each answer (slow lane) | recent transcript → facts, slot updates, register, deflection, lexical engagement | `app/observer.py`, `prompts/observer.md` |
| **Planner** (code, not an LLM) | after the observer | observer + signal tags + coverage → `Decision` (policy, goal, guidance) | `app/planner/` |
| **Speech habits** | after each session (`coverage_update`) | everything they've said → pet phrases with a rate, kept only if counted ≥3 times in ≥2 answers; stored as `speech_habit` notes, shown to the twin only. Same call picks 4–5 real Q→A pairs that best show their voice (code drops tired, short or split answers) → `voice_examples`, given to the twin as earlier chat turns | `app/offline/style.py`, `prompts/speech_habits.md` |
| **Twin** | creator asks | question + recalled corpus + style + speech habits → answer in the subject's usual voice | `app/twin.py`, `prompts/twin.md` |

Probes run offline after a session. They have two parts, with one prompt file each:

- **Question generator:** writes questions about a domain.
- **Judge:** checks whether the twin's answers are supported by the corpus.

## Planner policies

The planner picks one of these, in priority order:

1. **`wrap_up`:** past the target time, or the trend is "tired".
2. **`drop_topic`:** a deflection. The topic goes to never-raise for good.
3. **`let_them_run`:** they are engaged. Follow their thread and remember it as a bridge.
4. **`bridge`:** they've been engaged for 3 turns in a row (2 in the second half of the
   call). Use the warm moment to move to a new area, linked to what they just said. Without
   this, a happy person is never steered and the twin knows only one topic.
5. **`change_kind`:** flat for 2 or more of the last 3 turns. Switch to a choice or a different
   register.
6. **`continue`:** fill the next coverage goal, bridging from something they said.

Goals are chosen breadth first: an area nothing has been learned about yet beats finishing
one that's under way. A goal that hasn't come up after 90 s (60 s in the second half) is
dropped for the session; time, not turns, because one answer often arrives as several fragments. In the second half, if the goal's whole area is still untouched,
the tip stops being a hint and asks for it with the next question. A cut-off fragment
("Like, the whole") with no clear word signal is read as uncertain, never flat.

Signals only count once their confidence is at least 0.5. Cold-start tags, built on the session
baseline only, are capped at 0.4, so early on the planner relies on the observer's words.

## Voice specifics (GPT-Live)

- Guidance goes in as `session.thinking.append` (silent). The model is never interrupted.
- When GPT-Live delegates, the browser calls `/api/delegation`. The reply (relevant notes plus
  a tip) goes back as `session.commentary.append`.
- There is no server-side max duration. The browser hangs up at max + 60 s, and the server
  refuses turns after that.
