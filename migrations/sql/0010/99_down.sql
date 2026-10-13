-- 0010 / downgrade. The value 'chat_inline' STAYS in civ.fact_source: PostgreSQL cannot drop a value from an enum. Upgrading again is safe
-- (ADD VALUE IF NOT EXISTS). The full `downgrade base` drops the type.
-- The facts the chat wrote are DELETED: without source_message_id they could not satisfy the CHECK that the next `upgrade` adds again, and
-- versions are immutable, so they cannot be relabelled. (The same stance as the downgrade of 0008, which drops the invitations' notifications.)
-- Proposals of the quarantine that targeted such a fact go with it (ON DELETE CASCADE).
DELETE FROM civ.core_facts WHERE source::text = 'chat_inline';
--;;
CREATE OR REPLACE FUNCTION civ.core_facts_guard_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.source = 'approved_review' AND NOT EXISTS (
        SELECT 1 FROM civ.pending_facts p
        WHERE p.id = NEW.source_pending_id AND p.user_id = NEW.user_id
          AND p.subject = 'core_fact' AND p.change IN ('create', 'update') AND p.status = 'approved'
    ) THEN
        RAISE EXCEPTION 'core_facts: source=approved_review requires an APPROVED core_fact pending_facts row'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
--;;
ALTER TABLE civ.core_facts DROP CONSTRAINT IF EXISTS ck_core_facts_inline;
--;;
ALTER TABLE civ.core_facts DROP CONSTRAINT IF EXISTS fk_core_facts_source_message;
--;;
ALTER TABLE civ.core_facts DROP COLUMN IF EXISTS source_message_id;
--;;
ALTER TABLE civ.messages DROP CONSTRAINT IF EXISTS uq_messages_user_id_id;
