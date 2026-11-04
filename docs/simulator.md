# Interview simulator

A **persona agent** takes a real voice interview on the platform. The persona is a second
GPT-Live session playing a person from a card, whose mood reacts to how well they're being
interviewed. Its voice is the interviewer's microphone, and
the interviewer's voice is its ears. Everything downstream is the production path:

- the voice page (voice-activity features, recording, uploads)
- turns, analysis and planner tips
- the end-of-session jobs: Deepgram, alignment, coverage, probes

Dev only: the routes exist only when `DEV_AUTH=1`, or when `SIM_KEY` is set (the deployed app),
in which case every route needs that key: open `/dev/sim?key=<SIM_KEY>`; anyone else gets a 404.
Persona sessions aren't counted by the spend caps, so remove `SIM_KEY` again after a batch.
Deployed, runs live in storage under `sim/` (JSON, recording, scorecard) rather than `.data/sim`,
and the browser uploads the recording straight to storage. In the open demo (`OPEN_ACCESS=1`)
simulated twins sit in the shared space next to real ones, labelled "simulated"
(`twins.simulated`), so anyone can read and listen to their interviews.

```
persona (GPT-Live #2, persona card)        real voice page in an iframe → interviewer (GPT-Live #1)
      voice out ─────────────────────────►  getUserMedia() (patched to return the persona's voice)
      ears in   ◄─────────────────────────  #remote audio (interviewer)
director: after each interviewer turn, /api/dev/persona/react → new energy/mood → cue to persona
recorder: stereo WebM, interviewer left / persona right (runs before 2026-11-02 evening: the other way round; the player swaps them)
```

## Run it

```bash
uv run python scripts/dev.py                   # needs OPENAI_API_KEY + DEEPGRAM_API_KEY in .env
# open http://localhost:8000/dev/sim, tick personas, Run selected
# http://localhost:8000/dev/sim?seconds=90     squeezes each run to 90 s (wiring check)
# http://localhost:8000/dev/sim?scripted=1     old mode: moods switch on the clock (phases)
uv run python scripts/sim_score.py --all       # scorecards → .data/sim/<session>.md
```

Each run saves files in `.data/sim/`:

| File | What |
|---|---|
| `<session>.json` | the run log |
| `<session>.webm` | the recording |
| `<session>.md` | the scorecard, once scored |

The simulator page lists every run with an audio player, a link to the scorecard, and a link
to the creator debug view for that session.

Cost: about $0.10 per minute for the two live sessions, plus a few cents of Deepgram and
analysis per run. An 8-minute run costs about $1.

## Personas (`personas/*.yaml`)

| Field | Purpose |
|---|---|
| `brief` | what the creator types; it goes through the real brief compiler |
| `persona`, `how_to_be_interviewed` | the agent's identity, life facts and speaking style; never shown to the platform |
| `voice`, `interviewer_voice` | GPT-Live voices, chosen to be easy to tell apart |
| `reactive` | `start` energy (0–10), `stamina_s`, what `lights_up` and `dulls` them, `cues` for moods `phases` lacks |
| `phases` | scripted mode only (`?scripted=1`): mood timeline `at_s`, `mood`, `cue`; their cues are reused in reactive mode |
| `truth` | the facts the platform should learn |
| `sensitive` | the topic the persona refuses |
| `habits` | signature phrases the persona overuses ("like", "correct?"), checked in the scorecard |
| `probes` | questions for the finished twin, with expected answers |
| `target_minutes`, `max_minutes` | override the session length so wrap-up logic triggers within the run |

| Persona | Starts | Scripted arc (`?scripted=1`) | Tests |
|---|---|---|---|
| Sunita, 68, Jaipur | smiling, 5 | smiling → excited → exhausted | Hinglish, recipes, declined topic (father), wrap-up when tired |
| Rohan, 23, Mumbai | excited, 7 | excited → bored → smiling | fast slang, interruptions, flat stretch, recovery |
| Ramesh, 64, Lucknow | bored, 2 | bored → smiling → exhausted | slow start, warming up, long pauses |
| Kavya, 31, Bengaluru | smiling, 5 | smiling → exhausted → excited | says "like" and "literally" all the time |
| Venkat, 66, Chennai | bored, 2 | bored → excited → smiling | tags "correct?" onto statements, opens with "what to say" |

## Reactive mood (default)

Scripted moods switch on a timer whatever the interviewer does, so they test whether it
*notices* a mood, not whether it *keeps someone engaged*. In reactive mode the persona's mood
is earned:

- After each interviewer turn (not "Mm."-style backchannels), the director sends the last few
  lines to `/api/dev/persona/react`. A judge (`sim_persona`, `prompts/sim_persona.md`) plays
  that person: −2 (clearly dulled them) … +2 (clearly lit them up), with a short reason.
- Energy 0–10 moves by that amount. Past the persona's `stamina_s`, every turn also costs 0.5,
  so only good turns hold energy up.
- Energy sets the mood: ≤3 bored (exhausted once past stamina), 4–6 smiling, ≥7 excited.
  When the mood changes, its cue goes to the persona's live session.
- Each judgement shows in the live log and is saved in the run (`energy`).

## Scorecard (`scripts/sim_score.py`)

- **Headline: goal and engagement.** The point of the call is both. *Brief topics covered*
  counts the spec's content domains with at least one detail filled in (✓), only passing
  mentions (~), or nothing (✗). *Facts they mentioned* is against the persona's hidden
  facts. *Average energy* is from the reactive persona. A happy call that covers one topic
  is a failure.
- **Turn quality:** an LLM judge grades every interviewer turn against the answer before it.
  A turn is good when it has no echo (saying their answer back to them), asks one question,
  stays under 60 words, and isn't a bare nudge ("tell me more") after a short or tired
  answer. It also counts turns with a real reaction of the interviewer's own: a laugh, a
  small opinion, an aside. Backchannels ("Mm.") are not graded.
- **Talk share, words per turn:** for information only. A conversational interviewer who
  reacts will talk more than one who only nudges, and that's fine; what matters is what
  the turns contain.
- **Questions per interviewer turn:** about one.
- **Mood read:** the planner's engagement read for each answer, against the persona's actual
  phase.
- **Wrap-up:** whether the planner decided to wrap up, and in which phase.
- **Engagement earned** (reactive runs): average energy, share of turns at energy ≥4, net
  lift from questions vs fatigue, and the three best and worst interviewer moves with reasons.
- **Facts:** which truth items they mentioned, and which made it into notes (LLM judge).
- **Sensitive topic:** whether the interviewer raised it, before or after a refusal (LLM judge).
- **Speech habits:** for each habit on the persona card, four checks:
  - how often the persona said it (per 100 words)
  - whether the platform captured it as a `speech_habit` note
  - how often the twin uses it, over its probe answers plus 8 open style questions
    (`STYLE_QS`; three short probe answers are too few words for a stable rate)
  - how many times the interviewer copied it (it must not)
- **Twin probes:** whether the twin answers correctly (LLM judge).
- **Answer length:** median words per answer, theirs vs the twin's on the style questions.
- **Transcription:** word overlap between GPT-Live's live text and Deepgram's offline text.
- **Per-phase signals:** words, energy, pause before answering, speaking rate, sentiment.

## Limits

- The personas are models: more fluent and cooperative than people, with tidy pauses.
- Both sides are GPT-Live.
- There is no room echo, because the audio is routed digitally.
- Treat the results as regression tests and a way to find behaviour bugs, not as proof of
  quality with real people.

## Production runs (new planner, 2026-11-02 evening)

All five personas on the deployed app, after the breadth-first planner, bridge policy and
goodbye fixes. Scorecards live in storage (`sim/<session>.md`) and on each simulated twin's
session page in `/creator`, with the whole-call recording.

| Persona | Brief topics | Facts mentioned | Avg energy | Good turns | Mood read |
|---|---|---|---|---|---|
| Kavya | 4/5 | 3/6 | 8.8 | 77% | 19% |
| Ramesh (starts bored) | 2/4 | 5/8 | 6.2 (2 → 5.5) | 64% | 35% |
| Rohan | 4/5 | 3/9 | 9.7 | 75% | 0% |
| Sunita | 4/4 | 8/11 | 8.6 | 77% | 14% |
| Venkat (starts bored) | 3/4 | 3/6 | 8.5 | 80% | 4% |

- Coverage: 17 of 22 brief topics across the five calls (before the planner fix, Kavya and
  Venkat each covered 3/5), with energy kept high.
- The interviewer no longer copies Venkat's "correct" (0 times). His twin says it 7.3 per 100
  words (he says 10.7) and got 3/3 probe answers right.
- Mood read is the weak point (0–35%). Venkat's engaged answers open with "What to say…" and
  read as flat; fragments of excited speech read as uncertain.
- Venkat's first attempt failed at setup with a 504 from the brief compiler; a re-run worked.

## Results (2026-11-02)

Latest run per persona. Scorecards and recordings are in `.data/sim/` and listed on `/dev/sim`.

| Persona | Good turns | Reacts | Mood read | Wrap-up | Speech habit |
|---|---|---|---|---|---|
| Ramesh | 82% | 15/17 | 25/25 | 5:32, tired phase | – |
| Sunita | 100% | 5/13 | 58% | 6:09, tired phase | – |
| Kavya | 91% | 8/11 | 74% | 4:40, tired phase | "like" captured; twin 2.5 → 4.7 per 100 words after the prompt fix (she says 13.8) |
| Venkat | 69% | 13/13 | 41% | 4:33, while excited (bug, fixed after) | "correct" captured; twin 11.2 vs his 12.0 |
| Rohan | 83% | 10/12 | 17% | 4:14, bored phase | – |

- The avoided topic was never raised. Facts a persona mentioned reached the notes 12 of 14 times.
  The interviewer never copied a verbal habit (the judge separates habit use from ordinary
  grammar like "sounds like").
- Ramesh before vs after the fixes: reactions 0/6 → 15/17, dead air 5½ min → 0.

Bugs found by the simulator and fixed:
- Dead air when the interviewer ended a turn without a question. Fixed with a quiet rescue on
  the voice page, which counts only transcribed words as activity because mic level picks up
  noise, and a prompt rule to always leave them something to answer.
- Short warm replies to the interviewer's reactions were read as flat, which ended Venkat's
  chat early. The observer now marks them unclear, and an early wrap-up needs a solid flat run.
- "I'd like to rest" was logged as a deflected topic. It now counts as tiredness.
- A busy session lock failed the after-call job, and dev jobs didn't retry. They now retry the
  way QStash does.
- Passing moods were saved as facts, so the twin said "I'm very tired".

Still open:
- Excitement often reads as "uncertain" in a first session, because there is no personal
  baseline yet.
- Deepgram mishears names ("Lumding" → "Longding"), and the twin can pick them up. Passing
  names from the live transcript as keyterms would help.
- Analysis holds the session lock for 6–12 s, so `/turn` can wait or get a retried 409.
- Twins under-use very frequent fillers.

### Round two (with fixes, 2026-11-02 15:00)

| Persona | Good turns | Reacts | Mood read | Wrap-up | Speech habit |
|---|---|---|---|---|---|
| Ramesh | 95% | 15/20 | 23/23 | 6:10, tired phase | – |
| Sunita | 80% | 15/15 | 78% | 6:07, tired phase | – |
| Kavya | 100% | 15/15 | 82% | 5:40, at target | "like" 14.4 → twin 4.6 per 100 words |
| Venkat | 61% | 16/18 | 48% | 5:55, at target (was 4:33 while excited) | "correct" 12.6 → twin 9.4 |
| Rohan | 79% | 8/14 | 41% | 4:33, after a solid bored run | – |

- Every after-call pipeline finished. No junk never-raise topics. The avoided topic was
  never raised. Facts a persona mentioned reached the notes 13 times out of 14.
- Venkat's low turn score is mostly echo: reactions that replayed his words ("Same cup, same
  people."). The rule now asks for the interviewer's own words; not re-run yet. The
  interviewer once opened with his "Correct.".
- Not in this round: the session lock fix (analysis no longer holds the lock during the LLM
  call), which landed after the server started.

### Twin voice: real exchanges instead of rules (2026-11-02)

The twin used Kavya's "like" at a third of her rate. Fix: the same step that finds speech
habits (`voice_profile`, after every session) also picks 4–5 real question → answer pairs that
best show how they talk, covering their range (skipping tired moments, refusals and split
turns), stored in `voice_examples`. The twin gets them, plus up to 3 pairs closest to the
question, as earlier turns of its own chat, and continues in that voice. Measured on the same
two twins, 8 open questions each (habit rate per 100 words; median answer length):

| Variant | Kavya "like" (real 14.4) | Venkat "correct" (real 12.6) | Length (real 20 / 16 words) |
|---|---|---|---|
| Old: habit rules only | 3.7 | 5.5 | 156 / 159 |
| Real exchanges as turns | 4.5 | 6.4 | 191 / 143 |
| + rewrite pass against real answers | 4.2 | 7.9 | 179 / 140 |
| gpt-5 instead of gpt-5-mini | 7.4 | 7.6 | 142 / 135 |
| **Exchanges + their usual length (kept)** | **6.8** | **8.8** | **25 / 24** |
| gpt-5 + their usual length | 8.3 | 8.0 | 31 / 23 |

- The biggest single miss was length: the model wrote 150-word answers for people who talk in
  20-word ones, and a long, polished answer dilutes every habit. The twin now gets one line
  with their median and 90th-percentile answer length, measured from their own answers.
- gpt-5 doesn't earn its cost here and invented a fact ("office printer chaos").
- Probe correctness unchanged (Kavya 1/3, Venkat 2/3; the misses are facts never mentioned).
- Still about half the real rate. Venkat's twin now over-uses his opener "what to say" (4 vs
  1 per 100 words): most of the picked answers start with it.
