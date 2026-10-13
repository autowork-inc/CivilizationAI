-- 0001 / downgrade - removes everything created by 0001 EXCEPT:
--   * schema civ        (it holds civ.alembic_version, which Alembic updates after the downgrade);
--   * schema extensions and the vector extension (may be shared with other objects / enabled from the dashboard);
--   * role ai_civ_app   (roles are cluster-wide; drop it manually if you really want to).

DROP VIEW IF EXISTS civ.effective_principles;
--;;
DROP TABLE IF EXISTS
    civ.events, civ.diplomat_audit_log, civ.access_grants, civ.contacts, civ.episodic_memories,
    civ.memory_outbox, civ.messages, civ.emotional_overrides, civ.emotional_states, civ.core_facts,
    civ.pending_facts, civ.review_batches, civ.personas, civ.users;
--;;
DROP FUNCTION IF EXISTS civ.resolve_tenant_id(bigint, bigint);
--;;
DROP FUNCTION IF EXISTS civ.current_tenant_id();
--;;
DROP FUNCTION IF EXISTS civ.touch_updated_at();
--;;
DROP FUNCTION IF EXISTS civ.validate_timezone();
--;;
DROP FUNCTION IF EXISTS civ.pending_facts_guard();
--;;
DROP FUNCTION IF EXISTS civ.core_facts_guard_update();
--;;
DROP FUNCTION IF EXISTS civ.core_facts_guard_insert();
--;;
DROP FUNCTION IF EXISTS civ.emotional_states_guard_insert();
--;;
DROP FUNCTION IF EXISTS civ.emotional_overrides_guard_insert();
--;;
DROP TYPE IF EXISTS
    civ.fact_category, civ.fact_source, civ.emotion_source, civ.emotion_status, civ.override_mode,
    civ.pending_status, civ.pending_subject, civ.change_type, civ.outbox_status, civ.outbox_operation,
    civ.contact_status, civ.acl_scope, civ.event_status, civ.event_visibility, civ.episode_kind,
    civ.sensitivity, civ.message_role;
--;;
REVOKE USAGE ON SCHEMA civ FROM ai_civ_app;
--;;
REVOKE USAGE ON SCHEMA extensions FROM ai_civ_app;
--;;
ALTER ROLE ai_civ_app RESET search_path;
