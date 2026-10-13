-- 0001 / 01 - foundation: schemas, pgvector, application role, enum types, helper functions.
-- Statements are separated by a line containing only "--;;" (see migrations/versions/0001_memory_core.py).
-- The migration sets: SET LOCAL search_path TO civ, extensions, public.

CREATE SCHEMA IF NOT EXISTS civ;
--;;
CREATE SCHEMA IF NOT EXISTS extensions;
--;;
-- On Supabase the extension normally lives in schema "extensions"; if it is already installed
-- elsewhere (e.g. enabled from the dashboard) this is a no-op and search_path still resolves it.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
--;;

-- Runtime role used by the application. NOLOGIN here on purpose: no secrets in migrations.
-- Enable login once, manually (see README): ALTER ROLE ai_civ_app WITH LOGIN PASSWORD '...';
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ai_civ_app') THEN
        CREATE ROLE ai_civ_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;
    END IF;
    -- Fail-fast: RLS is meaningless if the runtime role can bypass it.
    IF EXISTS (SELECT FROM pg_catalog.pg_roles
               WHERE rolname = 'ai_civ_app' AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'role ai_civ_app must not be SUPERUSER or BYPASSRLS';
    END IF;
END
$$;
--;;
ALTER ROLE ai_civ_app SET search_path = civ, extensions, public;
--;;
GRANT USAGE ON SCHEMA civ TO ai_civ_app;
--;;
GRANT USAGE ON SCHEMA extensions TO ai_civ_app;
--;;
-- The GRANT above is a silent no-op (only a WARNING) when the migration role does not own the schema
-- "extensions" and has no grant option. Without USAGE the runtime role cannot resolve vector types/operators,
-- so fail here instead of at runtime.
DO $$
BEGIN
    IF NOT has_schema_privilege('ai_civ_app', 'extensions', 'USAGE') THEN
        RAISE EXCEPTION 'ai_civ_app has no USAGE on schema "extensions" (Supabase grants it to PUBLIC by default). As the owner of that schema run: GRANT USAGE ON SCHEMA extensions TO PUBLIC; then re-run the migration';
    END IF;
END
$$;
--;;

-- ============ Enum types ============
CREATE TYPE civ.fact_category     AS ENUM ('identity','body','principle','preference','goal','relationship','health','other');
--;;
CREATE TYPE civ.fact_source       AS ENUM ('onboarding','user_manual','approved_review');
--;;
CREATE TYPE civ.emotion_source    AS ENUM ('user_declared','inferred','user_confirmed');
--;;
CREATE TYPE civ.emotion_status    AS ENUM ('active','expired','dismissed');
--;;
CREATE TYPE civ.override_mode     AS ENUM ('suspend','soften');
--;;
CREATE TYPE civ.pending_status    AS ENUM ('pending','approved','rejected','expired');
--;;
CREATE TYPE civ.pending_subject   AS ENUM ('core_fact','emotional_state');
--;;
CREATE TYPE civ.change_type       AS ENUM ('create','update','delete');
--;;
CREATE TYPE civ.outbox_status     AS ENUM ('pending','processing','done','failed','dead');
--;;
CREATE TYPE civ.outbox_operation  AS ENUM ('compress_day','embed_episode','extract_facts');
--;;
CREATE TYPE civ.contact_status    AS ENUM ('pending','accepted','blocked');
--;;
CREATE TYPE civ.acl_scope         AS ENUM ('emotional_state_label','calendar_free_busy','calendar_details','profile_public','persona_status');
--;;
CREATE TYPE civ.event_status      AS ENUM ('planned','done','missed','cancelled','rescheduled');
--;;
CREATE TYPE civ.event_visibility  AS ENUM ('private','busy_only','shared');
--;;
CREATE TYPE civ.episode_kind      AS ENUM ('dialogue_chunk','daily_summary','event_note');
--;;
CREATE TYPE civ.sensitivity       AS ENUM ('normal','private','secret');
--;;
CREATE TYPE civ.message_role      AS ENUM ('user','assistant','system');
--;;

-- ============ Helper functions ============

-- Current tenant, taken from the transaction-local GUC app.user_id.
-- current_setting() WITHOUT missing_ok: if the GUC was never set it raises (fail-fast, no silent empty result).
-- If a pooled connection previously held a tenant, the value reads back as '' and the uuid cast raises as well.
CREATE FUNCTION civ.current_tenant_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT current_setting('app.user_id')::uuid $$;
--;;
CREATE FUNCTION civ.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;
--;;
-- Only named IANA zones from the PostgreSQL tz database are accepted ('UTC+3'-style POSIX strings are rejected
-- because they silently mean the opposite offset).
CREATE FUNCTION civ.validate_timezone() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = NEW.timezone) THEN
        RAISE EXCEPTION 'invalid IANA timezone: %', NEW.timezone USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN NEW;
END
$$;
