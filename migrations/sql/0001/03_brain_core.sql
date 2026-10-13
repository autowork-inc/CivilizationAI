-- 0001 / 03 - Tier 1 (Core Profile), Emotional Overrides, quarantine (pending_facts).
-- Creation order resolves a cycle: pending_facts <-> core_facts (target FK is added at the end).

CREATE TABLE civ.review_batches (
    id                  uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id             uuid        NOT NULL,
    sent_at             timestamptz,
    telegram_message_id bigint,
    closed_at           timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_review_batches PRIMARY KEY (id),
    CONSTRAINT fk_review_batches_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_review_batches_user_id_id UNIQUE (user_id, id),
    CONSTRAINT ck_review_batches_closed CHECK (closed_at IS NULL OR (sent_at IS NOT NULL AND closed_at >= sent_at))
);
--;;

-- ============ Quarantine: facts and emotional states proposed by the Garbage Collector ============
-- Nothing proposed here is visible to the prompt builder until the USER approves it.
CREATE TABLE civ.pending_facts (
    id                   uuid                NOT NULL DEFAULT gen_random_uuid(),
    user_id              uuid                NOT NULL,
    subject              civ.pending_subject NOT NULL DEFAULT 'core_fact',
    change               civ.change_type     NOT NULL,
    target_fact_id       uuid,                          -- core_facts row being updated/deleted
    -- proposal of a core fact (subject = 'core_fact')
    category             civ.fact_category,
    key                  text,
    value_text           text,
    value_num            numeric,
    value_unit           text,
    value_json           jsonb,
    priority             smallint,
    -- proposal of an emotional state (subject = 'emotional_state'); priority may be up to 100 here:
    -- this is exactly the path for AI-inferred emotions that must not apply without the user's consent.
    emotion_label        text,
    emotion_description  text,
    emotion_intensity    smallint,
    emotion_priority     smallint,
    emotion_expires_at   timestamptz,
    emotion_override_keys text[],                       -- principle keys; resolved to current versions on approval
    -- provenance
    evidence             text                NOT NULL,
    source_message_ids   uuid[]              NOT NULL,
    extractor_confidence numeric,
    -- moderation
    status               civ.pending_status  NOT NULL DEFAULT 'pending',
    review_batch_id      uuid,
    decided_at           timestamptz,
    idempotency_key      text                NOT NULL,  -- GC retries must not create duplicates
    created_at           timestamptz         NOT NULL DEFAULT now(),
    CONSTRAINT pk_pending_facts PRIMARY KEY (id),
    CONSTRAINT fk_pending_facts_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_pending_facts_batch FOREIGN KEY (user_id, review_batch_id) REFERENCES civ.review_batches (user_id, id),
    CONSTRAINT uq_pending_facts_user_id_id UNIQUE (user_id, id),
    CONSTRAINT uq_pending_facts_user_idem UNIQUE (user_id, idempotency_key),
    CONSTRAINT ck_pending_facts_decided CHECK ((status = 'pending') = (decided_at IS NULL)),
    CONSTRAINT ck_pending_facts_priority CHECK (priority IS NULL OR priority BETWEEN 0 AND 100),
    CONSTRAINT ck_pending_facts_emotion_ranges CHECK (
        (emotion_intensity IS NULL OR emotion_intensity BETWEEN 1 AND 10)
        AND (emotion_priority IS NULL OR emotion_priority BETWEEN 0 AND 100)),
    CONSTRAINT ck_pending_facts_confidence CHECK (extractor_confidence IS NULL OR extractor_confidence BETWEEN 0 AND 1),
    CONSTRAINT ck_pending_facts_evidence CHECK (char_length(btrim(evidence)) > 0 AND cardinality(source_message_ids) >= 1),
    CONSTRAINT ck_pending_facts_key_format CHECK (key IS NULL OR (char_length(key) <= 100 AND key ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$')),
    CONSTRAINT ck_pending_facts_core_fact_payload CHECK (
        subject <> 'core_fact' OR (
            emotion_label IS NULL AND emotion_description IS NULL AND emotion_intensity IS NULL
            AND emotion_priority IS NULL AND emotion_expires_at IS NULL AND emotion_override_keys IS NULL
            AND CASE change
                    WHEN 'create' THEN category IS NOT NULL AND key IS NOT NULL AND target_fact_id IS NULL
                                       AND num_nonnulls(value_text, value_num, value_json) >= 1
                    WHEN 'update' THEN category IS NOT NULL AND key IS NOT NULL AND target_fact_id IS NOT NULL
                                       AND num_nonnulls(value_text, value_num, value_json) >= 1
                    WHEN 'delete' THEN target_fact_id IS NOT NULL
                END
        )),
    CONSTRAINT ck_pending_facts_emotion_payload CHECK (
        subject <> 'emotional_state' OR (
            change = 'create'
            AND emotion_label IS NOT NULL AND emotion_intensity IS NOT NULL
            AND emotion_priority IS NOT NULL AND emotion_expires_at IS NOT NULL
            AND category IS NULL AND key IS NULL AND target_fact_id IS NULL AND priority IS NULL
            AND num_nonnulls(value_text, value_num, value_unit, value_json) = 0
        ))
);
--;;
CREATE INDEX ix_pending_facts_user_status ON civ.pending_facts (user_id, status, created_at);
--;;
-- A decision is final: approved / rejected / expired rows are immutable.
CREATE FUNCTION civ.pending_facts_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'pending_facts % is already % and can no longer change', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;
--;;
CREATE TRIGGER trg_pending_facts_guard
    BEFORE UPDATE ON civ.pending_facts
    FOR EACH ROW EXECUTE FUNCTION civ.pending_facts_guard();
--;;

-- ============ Tier 1: Core Profile (versioned hard facts) ============
CREATE TABLE civ.core_facts (
    id                uuid               NOT NULL DEFAULT gen_random_uuid(),
    user_id           uuid               NOT NULL,
    persona_id        uuid,                              -- NULL = global fact
    category          civ.fact_category  NOT NULL,
    key               text               NOT NULL,       -- 'weight', 'principle.no_danish'
    value_text        text,
    value_num         numeric,                           -- numbers live here, never only in free text
    value_unit        text,                              -- 'kg', 'cm'
    value_json        jsonb,
    priority          smallint           NOT NULL DEFAULT 50,
    source            civ.fact_source    NOT NULL,
    source_pending_id uuid,                              -- review decision this version came from
    valid_from        timestamptz        NOT NULL DEFAULT now(),
    valid_to          timestamptz,                       -- NULL = current version
    created_at        timestamptz        NOT NULL DEFAULT now(),
    CONSTRAINT pk_core_facts PRIMARY KEY (id),
    CONSTRAINT fk_core_facts_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_core_facts_persona FOREIGN KEY (user_id, persona_id)
        REFERENCES civ.personas (user_id, id) ON DELETE SET NULL (persona_id),
    CONSTRAINT fk_core_facts_source_pending FOREIGN KEY (user_id, source_pending_id)
        REFERENCES civ.pending_facts (user_id, id),
    CONSTRAINT uq_core_facts_user_id_id UNIQUE (user_id, id),
    CONSTRAINT ck_core_facts_priority CHECK (priority BETWEEN 0 AND 100),
    CONSTRAINT ck_core_facts_value CHECK (num_nonnulls(value_text, value_num, value_json) >= 1),
    CONSTRAINT ck_core_facts_validity CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ck_core_facts_key_format CHECK (char_length(key) <= 100 AND key ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$'),
    CONSTRAINT ck_core_facts_review_link CHECK ((source = 'approved_review') = (source_pending_id IS NOT NULL))
);
--;;
-- Exactly one CURRENT version per (tenant, key); history stays in the same table.
CREATE UNIQUE INDEX uq_core_facts_current_key ON civ.core_facts (user_id, key) WHERE valid_to IS NULL;
--;;
CREATE INDEX ix_core_facts_user_key_history ON civ.core_facts (user_id, key, valid_from);
--;;
-- A fact version is immutable. Allowed changes: closing it (valid_to NULL -> timestamp) and detaching it from a
-- deleted persona (persona_id -> NULL, performed by the ON DELETE SET NULL (persona_id) foreign key action;
-- that action is an UPDATE and fires this trigger too). The content of a fact never changes.
CREATE FUNCTION civ.core_facts_guard_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF (to_jsonb(NEW) - 'valid_to' - 'persona_id') IS DISTINCT FROM (to_jsonb(OLD) - 'valid_to' - 'persona_id')
       OR (NEW.persona_id IS DISTINCT FROM OLD.persona_id AND NEW.persona_id IS NOT NULL) THEN
        RAISE EXCEPTION 'core_facts are versioned: only valid_to (and clearing persona_id) may change, insert a new version instead'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.valid_to IS NOT NULL AND NEW.valid_to IS DISTINCT FROM OLD.valid_to THEN
        RAISE EXCEPTION 'core_facts version % is already closed', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;
--;;
CREATE TRIGGER trg_core_facts_guard_update
    BEFORE UPDATE ON civ.core_facts
    FOR EACH ROW EXECUTE FUNCTION civ.core_facts_guard_update();
--;;
-- Human-in-the-loop at the DB level: a fact "from review" must point to an APPROVED proposal.
CREATE FUNCTION civ.core_facts_guard_insert() RETURNS trigger
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
CREATE TRIGGER trg_core_facts_guard_insert
    BEFORE INSERT ON civ.core_facts
    FOR EACH ROW EXECUTE FUNCTION civ.core_facts_guard_insert();
--;;
ALTER TABLE civ.pending_facts
    ADD CONSTRAINT fk_pending_facts_target FOREIGN KEY (user_id, target_fact_id)
        REFERENCES civ.core_facts (user_id, id) ON DELETE CASCADE;
--;;

-- ============ Emotional states ("chemistry") ============
CREATE TABLE civ.emotional_states (
    id                uuid                NOT NULL DEFAULT gen_random_uuid(),
    user_id           uuid                NOT NULL,
    label             text                NOT NULL,
    description       text,
    intensity         smallint            NOT NULL,
    priority          smallint            NOT NULL,
    source            civ.emotion_source  NOT NULL,
    source_pending_id uuid,
    status            civ.emotion_status  NOT NULL DEFAULT 'active',
    started_at        timestamptz         NOT NULL DEFAULT now(),
    expires_at        timestamptz         NOT NULL,      -- emotions are always temporary
    created_at        timestamptz         NOT NULL DEFAULT now(),
    CONSTRAINT pk_emotional_states PRIMARY KEY (id),
    CONSTRAINT fk_emotional_states_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_emotional_states_source_pending FOREIGN KEY (user_id, source_pending_id)
        REFERENCES civ.pending_facts (user_id, id),
    CONSTRAINT uq_emotional_states_user_id_id UNIQUE (user_id, id),
    CONSTRAINT ck_emotional_states_intensity CHECK (intensity BETWEEN 1 AND 10),
    CONSTRAINT ck_emotional_states_priority CHECK (priority BETWEEN 0 AND 100),
    -- An emotion inferred by the AI can never outweigh a principle with priority >= 60;
    -- anything stronger must go through quarantine (pending_facts) and be confirmed by the user.
    CONSTRAINT ck_emotional_states_inferred_cap CHECK (source <> 'inferred' OR priority <= 60),
    CONSTRAINT ck_emotional_states_expiry CHECK (expires_at > started_at),
    CONSTRAINT ck_emotional_states_review_link CHECK ((source = 'user_confirmed') = (source_pending_id IS NOT NULL))
);
--;;
CREATE INDEX ix_emotional_states_user_active ON civ.emotional_states (user_id, expires_at) WHERE status = 'active';
--;;
CREATE FUNCTION civ.emotional_states_guard_insert() RETURNS trigger
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
CREATE TRIGGER trg_emotional_states_guard_insert
    BEFORE INSERT ON civ.emotional_states
    FOR EACH ROW EXECUTE FUNCTION civ.emotional_states_guard_insert();
--;;

CREATE TABLE civ.emotional_overrides (
    id                 uuid              NOT NULL DEFAULT gen_random_uuid(),
    user_id            uuid              NOT NULL,
    emotional_state_id uuid              NOT NULL,
    core_fact_id       uuid              NOT NULL,       -- the principle VERSION being overridden
    mode               civ.override_mode NOT NULL DEFAULT 'suspend',
    note               text,
    CONSTRAINT pk_emotional_overrides PRIMARY KEY (id),
    CONSTRAINT fk_emotional_overrides_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_emotional_overrides_state FOREIGN KEY (user_id, emotional_state_id)
        REFERENCES civ.emotional_states (user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_emotional_overrides_fact FOREIGN KEY (user_id, core_fact_id)
        REFERENCES civ.core_facts (user_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_emotional_overrides_state_fact UNIQUE (emotional_state_id, core_fact_id)
);
--;;
CREATE INDEX ix_emotional_overrides_user_fact ON civ.emotional_overrides (user_id, core_fact_id);
--;;
CREATE FUNCTION civ.emotional_overrides_guard_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM civ.core_facts f
        WHERE f.id = NEW.core_fact_id AND f.user_id = NEW.user_id
          AND f.category = 'principle' AND f.valid_to IS NULL
    ) THEN
        RAISE EXCEPTION 'emotional_overrides: target must be a CURRENT fact of category principle'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
--;;
CREATE TRIGGER trg_emotional_overrides_guard_insert
    BEFORE INSERT ON civ.emotional_overrides
    FOR EACH ROW EXECUTE FUNCTION civ.emotional_overrides_guard_insert();
--;;

-- ============ Effective principles: who wins is decided by SQL, not by the LLM ============
-- security_invoker = true (PostgreSQL 15+): the view runs with the caller's rights, so tenant RLS on the
-- underlying tables still applies. Without it a view would run as its owner and bypass RLS.
CREATE VIEW civ.effective_principles WITH (security_invoker = true) AS
SELECT f.user_id,
       f.id          AS fact_id,
       f.persona_id,
       f.key,
       f.value_text,
       f.value_num,
       f.value_unit,
       f.value_json,
       f.priority,
       (ov.override_mode IS NOT NULL) AS is_overridden,
       ov.override_mode,
       ov.overridden_by,
       ov.override_priority,
       ov.override_note
FROM civ.core_facts f
LEFT JOIN LATERAL (
    SELECT o.mode     AS override_mode,
           s.label    AS overridden_by,
           s.priority AS override_priority,
           o.note     AS override_note
    FROM civ.emotional_overrides o
    JOIN civ.emotional_states s ON s.user_id = o.user_id AND s.id = o.emotional_state_id
    WHERE o.user_id = f.user_id
      AND o.core_fact_id = f.id
      AND s.status = 'active'
      AND s.started_at <= now()
      AND s.expires_at > now()
      AND s.priority > f.priority              -- the "chemistry" rule: strictly higher priority wins
    ORDER BY s.priority DESC, s.started_at DESC, s.id
    LIMIT 1
) ov ON true
WHERE f.category = 'principle' AND f.valid_to IS NULL;
