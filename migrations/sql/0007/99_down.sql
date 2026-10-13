-- 0007 / downgrade
DROP FUNCTION IF EXISTS civ.diplomat_mood(uuid);
--;;
DROP FUNCTION IF EXISTS civ.diplomat_free_busy(uuid, timestamptz, timestamptz);
--;;
DROP FUNCTION IF EXISTS civ.diplomat_authorize(uuid, civ.acl_scope);
--;;
DROP INDEX IF EXISTS civ.ix_diplomat_audit_log_pair;
--;;
ALTER TABLE civ.messages DROP COLUMN third_party_data;
--;;
CREATE OR REPLACE FUNCTION civ.emotional_states_guard_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.source = 'user_confirmed' AND NOT EXISTS (
        SELECT 1 FROM civ.pending_facts p
        WHERE p.id = NEW.source_pending_id AND p.user_id = NEW.user_id
          AND p.subject = 'emotional_state' AND p.status = 'approved'
    ) THEN
        RAISE EXCEPTION 'emotional_states: source=user_confirmed requires an APPROVED emotional_state pending_facts row'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
--;;
ALTER TABLE civ.emotional_states DROP CONSTRAINT ck_emotional_states_mood_confirmed;
--;;
ALTER TABLE civ.emotional_states DROP COLUMN mood;
--;;
ALTER TABLE civ.pending_facts DROP CONSTRAINT ck_pending_facts_mood_subject;
--;;
ALTER TABLE civ.pending_facts DROP COLUMN emotion_mood;
--;;
DROP TYPE civ.shared_mood;
