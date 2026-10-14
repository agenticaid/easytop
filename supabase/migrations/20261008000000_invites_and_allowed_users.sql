-- Invite links and the stored allowlist (Phase 6). NOT yet applied to easytop-dev or easytop-prod.
-- Server-only like every other table: RLS on with no policies AND all privileges revoked from anon/authenticated
-- (Supabase grants them everything on new public tables). No Webtop content, no invite code: only its hash.

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
