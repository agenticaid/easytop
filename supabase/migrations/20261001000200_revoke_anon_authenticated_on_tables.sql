-- Applied to easytop-dev 2026-10-01 (migration name: revoke_anon_authenticated_on_tables).
-- Supabase grants every privilege on new public tables to anon and authenticated. RLS (enabled, no policies)
-- already hides all rows from them through the Data API, but TRUNCATE and the other table-level privileges
-- are not row-level. Easytop is server-only: the bot connects with the service role, which keeps its
-- privileges. Apply the same revoke to easytop-prod at task 6.2.
revoke all on table
  public.parents,
  public.parent_channels,
  public.kids,
  public.webtop_sessions,
  public.digest_settings
from anon, authenticated;
