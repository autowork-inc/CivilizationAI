-- 0001 / 06 - Row Level Security, privileges, self-check.
--
-- Model: ENABLE (not FORCE) ROW LEVEL SECURITY + a dedicated runtime role (ai_civ_app) that is NOT the table owner
-- and has no BYPASSRLS. The owner/migration role bypasses RLS, which is what lets the SECURITY DEFINER functions
-- (resolve_tenant_id, future Diplomat) see across tenants. The application verifies at startup that it is NOT
-- connected as a role that bypasses RLS (ai_civ.db.tenancy.verify_rls_enforced).
-- Tenant context: SELECT set_config('app.user_id', '<uuid>', true) inside the transaction (is_local = true).

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'personas', 'review_batches', 'pending_facts', 'core_facts', 'emotional_states', 'emotional_overrides',
        'messages', 'memory_outbox', 'episodic_memories', 'contacts', 'access_grants', 'events'
    ] LOOP
        EXECUTE format('ALTER TABLE civ.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON civ.%I AS PERMISSIVE FOR ALL TO ai_civ_app '
            'USING (user_id = (SELECT civ.current_tenant_id())) '
            'WITH CHECK (user_id = (SELECT civ.current_tenant_id()))', t);
    END LOOP;
END
$$;
--;;
-- users: the row IS the tenant
ALTER TABLE civ.users ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.users AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (id = (SELECT civ.current_tenant_id()))
    WITH CHECK (id = (SELECT civ.current_tenant_id()));
--;;
-- audit log: the data owner may read it; nobody but SECURITY DEFINER functions may write it
ALTER TABLE civ.diplomat_audit_log ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY owner_read ON civ.diplomat_audit_log AS PERMISSIVE FOR SELECT TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()));
--;;

-- ============ Privileges (explicit list: alembic_version and future tables get nothing by default) ============
GRANT SELECT, INSERT, UPDATE, DELETE ON
    civ.users, civ.personas, civ.review_batches, civ.pending_facts, civ.core_facts, civ.emotional_states,
    civ.emotional_overrides, civ.messages, civ.memory_outbox, civ.episodic_memories, civ.contacts,
    civ.access_grants, civ.events
    TO ai_civ_app;
--;;
GRANT SELECT ON civ.diplomat_audit_log TO ai_civ_app;
--;;
GRANT SELECT ON civ.effective_principles TO ai_civ_app;
--;;
REVOKE ALL ON FUNCTION civ.current_tenant_id() FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.current_tenant_id() TO ai_civ_app;
--;;
REVOKE ALL ON FUNCTION civ.resolve_tenant_id(bigint, bigint) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.resolve_tenant_id(bigint, bigint) TO ai_civ_app;
--;;

-- ============ Self-check: the migration fails if any table was left without RLS ============
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'civ' AND c.relkind = 'r' AND c.relname <> 'alembic_version' AND NOT c.relrowsecurity
    LOOP
        RAISE EXCEPTION 'table civ.% has no row level security', r.relname;
    END LOOP;
END
$$;
