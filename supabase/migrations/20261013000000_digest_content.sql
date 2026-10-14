-- How much the evening reminder says (Erez 2026-10-13): none = the fixed content-free message, due = what is due
-- tomorrow, all = also the day's other parts. Only the level is stored, never any school content.
alter table public.digest_settings
  add column content text not null default 'none'
  check (content in ('none', 'due', 'all'));
