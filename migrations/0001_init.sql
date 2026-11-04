-- Initial schema (plan v3, section 6.2).
create extension if not exists vector;

-- people & access
create table creators (
  id uuid primary key,                         -- = supabase auth user id
  display_name text,
  created_at timestamptz default now()
);

create table twins (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references creators(id),
  subject_name text not null,
  status text not null default 'draft',        -- draft | active | archived
  created_at timestamptz default now()
);

create table twin_specs (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  version int not null,
  spec jsonb not null,
  brief_text text,
  approved_at timestamptz,
  created_at timestamptz default now(),
  unique (twin_id, version)
);

create table subject_invites (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  token_hash text not null unique,
  label text,
  expires_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz default now()
);

-- conversations
create table sessions (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  twin_spec_id uuid not null references twin_specs(id),
  invite_id uuid references subject_invites(id),
  modality text not null,                      -- voice | text
  status text not null default 'active',       -- active | ended | processing | processed | failed
  end_reason text,
  started_at timestamptz default now(),
  last_activity_at timestamptz default now(),
  ended_at timestamptz,
  duration_s numeric,
  max_duration_s int not null,
  expected_segments int,
  gptlive_session_ref text,
  stats jsonb not null default '{}'
);

create table turns (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references sessions(id),
  twin_id uuid not null references twins(id),
  idx int not null,
  client_turn_id text not null,
  speaker text not null,                       -- subject | interviewer
  start_ms int,
  end_ms int,
  created_at timestamptz default now(),
  text_live text,
  text_corpus text,
  words jsonb,
  features jsonb,                              -- browser / text features sent with the turn
  register text,
  topics text[],
  analyzed_at timestamptz,
  unique (session_id, idx),
  unique (session_id, client_turn_id, speaker)
);

create table imports (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  kind text not null,                          -- whatsapp | email | doc | other
  author_name text,                            -- whose lines are the subject's, for chat exports
  storage_path text,
  status text not null default 'pending',
  created_at timestamptz default now()
);

create table corpus_items (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  source text not null,                        -- interview | import
  modality text not null,                      -- voice | text
  turn_id uuid references turns(id),
  import_id uuid references imports(id),
  text text not null,
  register text,
  addressee text,
  embedding vector(1536),
  created_at timestamptz default now(),
  unique (turn_id)
);

-- analysis
create table observer_outputs (
  turn_id uuid primary key references turns(id),
  output jsonb not null,
  model text,
  latency_ms int,
  created_at timestamptz default now()
);

create table signals (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references sessions(id),
  turn_id uuid references turns(id),
  kind text not null,
  source text not null,                        -- browser | deepgram_timing | text | worker
  value jsonb not null,
  confidence real,
  created_at timestamptz default now()
);

create table baselines (
  twin_id uuid not null references twins(id),
  modality text not null,
  source text not null,
  features jsonb not null,
  n_turns int not null,
  updated_at timestamptz default now(),
  primary key (twin_id, modality, source)
);

-- planner
create table notes (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  turn_id uuid references turns(id),
  kind text not null,                          -- fact | preference | person | story_hook | followup | speech_habit
  text text not null,
  embedding vector(1536),
  created_at timestamptz default now()
);

create table planner_snapshots (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  session_id uuid not null references sessions(id),
  turn_id uuid references turns(id),
  state jsonb not null,
  decision jsonb not null,
  created_at timestamptz default now()
);

create table coverage_slots (
  twin_id uuid not null references twins(id),
  domain_id text not null,
  item text not null default '',
  slot text not null,
  status text not null default 'empty',        -- empty | partial | done
  evidence uuid[] not null default '{}',
  updated_at timestamptz default now(),
  primary key (twin_id, domain_id, item, slot)
);

create table register_coverage (
  twin_id uuid not null references twins(id),
  register text not null,
  modality text not null,
  word_count int not null default 0,
  feature_history jsonb not null default '[]',
  done boolean not null default false,
  primary key (twin_id, register, modality)
);

create table interests (
  twin_id uuid not null references twins(id),
  topic text not null,
  score real not null,
  n_obs int not null,
  last_seen timestamptz,
  primary key (twin_id, topic)
);

create table do_not_raise (
  twin_id uuid not null references twins(id),
  topic text not null,
  reason text,
  created_at timestamptz default now(),
  primary key (twin_id, topic)
);

-- media & jobs
create table audio_assets (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references sessions(id),
  kind text not null,                          -- segment | merged
  seq int not null,
  start_ms int not null,
  end_ms int,
  storage_path text not null,
  bytes bigint,
  transcript_status text not null default 'pending',
  transcript_path text,
  created_at timestamptz default now(),
  unique (session_id, kind, seq)
);

create table jobs (
  id uuid primary key default gen_random_uuid(),
  kind text not null,
  ref_id uuid not null,
  status text not null default 'queued',       -- queued | running | done | failed
  attempts int not null default 0,
  idempotency_key text unique,
  last_error text,
  payload jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- evaluation
create table labels (
  turn_id uuid primary key references turns(id),
  label text not null,                         -- engaged | flat | hesitant | laughing | unclear
  labeled_by uuid references creators(id),
  created_at timestamptz default now()
);

create table eval_runs (
  id uuid primary key default gen_random_uuid(),
  twin_id uuid not null references twins(id),
  kind text not null,                          -- probe_test | discriminator | signal_validation | asr_benchmark
  result jsonb not null,
  created_at timestamptz default now()
);

-- indexes
create index on turns (twin_id);
create index turns_unanalyzed on turns (session_id) where analyzed_at is null and speaker = 'subject';
create index on signals (turn_id);
create index on signals (session_id);
create index on notes (twin_id);
create index on corpus_items (twin_id);
create index on jobs (status, kind);
create index on sessions (status, last_activity_at);
create index on sessions (twin_id, started_at);
-- add once corpus_items > ~1k rows:
-- create index on corpus_items using hnsw (embedding vector_cosine_ops);

-- Access model: only the API (server role) touches tables. RLS on, no policies = deny for
-- anon/authenticated if the anon key leaks. Skipped on plain Postgres where roles don't exist.
do $$
declare t text;
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    foreach t in array array['creators','twins','twin_specs','subject_invites','sessions','turns',
      'imports','corpus_items','observer_outputs','signals','baselines','notes','planner_snapshots',
      'coverage_slots','register_coverage','interests','do_not_raise','audio_assets','jobs',
      'labels','eval_runs'] loop
      execute format('alter table %I enable row level security', t);
    end loop;
  end if;
end $$;
