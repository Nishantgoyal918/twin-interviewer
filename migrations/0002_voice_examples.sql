-- Real question → answer pairs that best show how a person talks, picked after each session
-- together with their speech habits. The twin gets them as earlier turns of its own chat.
create table voice_examples (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  turn_id uuid references turns(id),
  question text,
  answer text not null,
  why text,
  created_at timestamptz default now()
);
create index voice_examples_twin on voice_examples (twin_id);
