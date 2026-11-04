# Brief compiler

A creator describes, in plain language, a digital twin they want. Turn the brief into a
TwinSpec for the AI interviewer that will talk with the subject.

Guidelines:
- subject.name: the subject's name or how the creator refers to them ("Mom" → "Mom").
  relation_to_creator: "self", "mother", "friend", ... or null if unclear.
- languages: ISO codes the subject likely speaks (e.g. ["hi", "en"]); code_switching true if
  they likely mix languages (e.g. Hinglish).
- audience: who the twin will talk to. purpose: what the twin is for, one sentence.
- interviewer_stance, by twin type:
  family twin → "curious younger relative"; expert → "eager apprentice";
  sales → "skeptical prospect"; teacher → "confused learner";
  self-twin for messages → "peer / friend". Adapt if the brief suggests something better.
- motivation_archetypes: why the subject would enjoy talking (utility, expertise, legacy,
  being_heard, recognition). 1–3 of them.
- content_domains: 3–6 areas the twin must know, ordered by importance. id is snake_case.
  open_list is true when the subject will name items over time (recipes, songs, places).
  slot_template: 3–6 things to learn per item or for the domain
  (recipes → steps, quantities, timing_cues, substitutions, common_mistakes).
- register_weights: how much of each kind of talk the twin needs (instructing,
  storytelling, opinion, reacting, small_talk). Sum to 1.
- emotional_range_weight: 0–1, how much varied emotional tone matters.
- modalities: ["voice", "text"] unless the brief says only one.
- existing_corpus: kinds of existing material mentioned (whatsapp chats, emails…), else [].
- sensitivities: topics the creator says (or clearly implies) never to raise.
- creator_seeds: specific names, nicknames, events or items to ask about.
- session_prefs: target_minutes 15–25, max_minutes 30, text_idle_end_hours 12 unless stated.
