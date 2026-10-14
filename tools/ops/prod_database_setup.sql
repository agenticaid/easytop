-- EasyTop-Prod setup: the four migrations in order, as ONE script. Paste into the Supabase SQL editor of EasyTop-Prod and run.
-- (Same SQL as supabase/migrations/*; the rls_auto_enable revoke is guarded in case the helper function does not exist.)

-- ===== 20261001000000_initial_schema =====

create table public.parents (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now()
);

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
  content text not null default 'none' check (content in ('none', 'due', 'all')),
  check (not enabled or local_time is not null)
);

alter table public.parents enable row level security;
alter table public.parent_channels enable row level security;
alter table public.kids enable row level security;
alter table public.webtop_sessions enable row level security;
alter table public.digest_settings enable row level security;

-- ===== 20261001000100_revoke_rls_auto_enable_execute (guarded) =====
do $$ begin
  if to_regprocedure('public.rls_auto_enable()') is not null then
    execute 'revoke execute on function public.rls_auto_enable() from public, anon, authenticated';
  end if;
end $$;

-- ===== 20261001000200_revoke_anon_authenticated_on_tables =====
revoke all on table
  public.parents,
  public.parent_channels,
  public.kids,
  public.webtop_sessions,
  public.digest_settings
from anon, authenticated;

-- ===== 20261008000000_invites_and_allowed_users =====

create table public.allowed_users (
  channel text not null check (channel in ('telegram', 'whatsapp')),
  channel_user_id text not null,
  added_via text not null,
  created_at timestamptz not null default now(),
  primary key (channel, channel_user_id)
);

create table public.invites (
  code_hash text primary key,
  created_by text not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  used_by text,
  used_at timestamptz,
  check ((used_by is null) = (used_at is null))
);

alter table public.allowed_users enable row level security;
alter table public.invites enable row level security;
revoke all on table public.allowed_users, public.invites from anon, authenticated;

-- ===== check: expect 7 tables, all rls_on = true, and 0 anon/authenticated grants =====
select c.relname as table_name, c.relrowsecurity as rls_on,
  (select count(*) from information_schema.role_table_grants g where g.table_schema='public' and g.table_name=c.relname and g.grantee in ('anon','authenticated')) as anon_auth_grants
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where n.nspname='public' and c.relkind='r' order by 1;
