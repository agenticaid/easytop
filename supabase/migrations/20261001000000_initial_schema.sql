-- Easytop initial schema. Written overnight 2026-10-01, reviewed and applied to easytop-dev the same day
-- (migration name: initial_schema). NOT yet applied to easytop-prod (task 6.2). Server-only access (service role); RLS is enabled with no
-- policies as a second layer, so anon/authenticated roles get nothing.
-- No Webtop content is stored here: only parent identity, confirmed kids, an encrypted session blob
-- (ciphertext only, key lives outside the DB) and digest settings.

create table public.parents (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now()
);

-- Keyed by parent, not channel: Telegram and (stage 2) WhatsApp identities map to the same parent.
create table public.parent_channels (
  id uuid primary key default gen_random_uuid(),
  parent_id uuid not null references public.parents (id) on delete cascade,
  channel text not null check (channel in ('telegram', 'whatsapp')),
  channel_user_id text not null,
  created_at timestamptz not null default now(),
  unique (channel, channel_user_id)
);
create index parent_channels_parent_id_idx on public.parent_channels (parent_id);

create table public.kids (
  id uuid primary key default gen_random_uuid(),
  parent_id uuid not null references public.parents (id) on delete cascade,
  webtop_kid_id text not null,
  name text not null,
  class_name text not null default '',
  school_id text not null,
  school_name text not null,
  context_ref text,
  confirmed_at timestamptz not null default now(),
  unique (parent_id, webtop_kid_id)
);
create index kids_parent_id_idx on public.kids (parent_id);

-- Ciphertext only. Never the password, never captcha data, never the encryption key.
create table public.webtop_sessions (
  parent_id uuid primary key references public.parents (id) on delete cascade,
  encrypted_session bytea not null,
  updated_at timestamptz not null default now()
);

create table public.digest_settings (
  parent_id uuid primary key references public.parents (id) on delete cascade,
  enabled boolean not null default false,
  local_time time,
  timezone text not null default 'Asia/Jerusalem',
  check (not enabled or local_time is not null)
);

alter table public.parents enable row level security;
alter table public.parent_channels enable row level security;
alter table public.kids enable row level security;
alter table public.webtop_sessions enable row level security;
alter table public.digest_settings enable row level security;
