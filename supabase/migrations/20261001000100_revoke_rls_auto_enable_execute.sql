-- Applied to easytop-dev 2026-10-01 (migration name: revoke_rls_auto_enable_execute).
-- public.rls_auto_enable() is a SECURITY DEFINER helper that already existed in the project (not ours).
-- The Supabase security advisor flagged it as callable by anon/authenticated through /rest/v1/rpc.
-- Nothing in Easytop calls it, so nobody but the owner needs EXECUTE.
revoke execute on function public.rls_auto_enable() from public, anon, authenticated;
