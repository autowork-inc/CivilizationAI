-- 0010: facts that the bot writes to Core straight from a conversation (the model's function save_fact), without waiting for the nightly GC.
-- This is a write to Core with no human review, so the database - not only the code - says what such a fact may be:
--   * the source is a new value of civ.fact_source, 'chat_inline';
--   * it must point to the USER message it came from (source_message_id; a composite FK keeps it inside the tenant, and a trigger checks
--     that the message is a user's, not the assistant's);
--   * its priority is at most 60 (the same ceiling as the AI-inferred emotions) and it is never a principle.
-- ALTER TYPE ... ADD VALUE may run inside the migration's transaction (PostgreSQL 12+), but the new value cannot be USED before the commit:
-- that is why the constraint and the trigger compare source::text instead of using an enum literal.
ALTER TYPE civ.fact_source ADD VALUE IF NOT EXISTS 'chat_inline';
--;;
-- the target of a composite tenant-safe FK (messages had only its primary key)
ALTER TABLE civ.messages ADD CONSTRAINT uq_messages_user_id_id UNIQUE (user_id, id);
--;;
ALTER TABLE civ.core_facts ADD COLUMN source_message_id uuid;
--;;
-- NO ACTION on purpose: a version is immutable (trigger), so the FK must never try to null the column; messages are never deleted
-- except together with the whole tenant (ON DELETE CASCADE from users removes both sides in one statement).
ALTER TABLE civ.core_facts
    ADD CONSTRAINT fk_core_facts_source_message FOREIGN KEY (user_id, source_message_id) REFERENCES civ.messages (user_id, id);
--;;
ALTER TABLE civ.core_facts
    ADD CONSTRAINT ck_core_facts_inline CHECK (
        source::text <> 'chat_inline'
        OR (priority <= 60 AND category <> 'principle' AND source_message_id IS NOT NULL)
    );
--;;
-- the guard of 0001 plus one clause: a chat_inline fact must come from a message of the USER in the same tenant
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
    IF NEW.source::text = 'chat_inline' AND NOT EXISTS (
        SELECT 1 FROM civ.messages m
        WHERE m.id = NEW.source_message_id AND m.user_id = NEW.user_id AND m.role = 'user'
    ) THEN
        RAISE EXCEPTION 'core_facts: source=chat_inline requires the USER message the fact came from'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
