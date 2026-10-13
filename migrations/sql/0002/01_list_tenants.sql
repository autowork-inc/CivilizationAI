-- 0002 / 01 - tenant enumeration for background workers (Celery Beat dispatchers).
--
-- civ.users is protected by RLS (id = current tenant), so the runtime role cannot enumerate tenants: without a tenant
-- context every query fails (fail-fast). Nightly jobs must visit every tenant, so - exactly like resolve_tenant_id in
-- 0001 - a SECURITY DEFINER function (runs as the table owner, which bypasses RLS because FORCE is not used) exposes
-- the MINIMUM: the tenant id and its time zone (needed to find the tenant's "yesterday"). Nothing else leaves.
-- Everything after that point goes through Database.tenant_session(<id>) and is subject to RLS again.

CREATE FUNCTION civ.list_tenants()
    RETURNS TABLE (tenant_id uuid, tenant_timezone text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
    SELECT u.id, u.timezone FROM civ.users u ORDER BY u.created_at, u.id
$$;
--;;
REVOKE ALL ON FUNCTION civ.list_tenants() FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.list_tenants() TO ai_civ_app;
