-- 0022: time capsules (v2.0, stage 4, PR 4.5).
--   * civ.time_capsules - a letter the user sealed for their future self: the words (message), when it must be delivered (deliver_at), and its life:
--                         pending -> delivered (by the Beat job, delivered_at) or canceled (by the user). A decided capsule is final (trigger).
--                         It is NOT a timer: deliver_at is at least 6 days after sealing (a date 7 days away at 09:00 is 6 days 21 hours away) and at most
--                         1830 days (5 years). claimed_at is the lease of the delivery job: a capsule being delivered is not picked up twice, a dead worker's lease expires.
--   * civ.time_capsules_due_tenants - whom the hourly job must visit: one indexed query for ALL tenants (like cards_due_tenants).
-- source_message_id is the Telegram message id of the sealing message (bigint), unique per tenant: a replayed update cannot seal a second capsule.

CREATE TABLE civ.time_capsules (
    id                uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id           uuid        NOT NULL,
    title             text,
    message           text        NOT NULL,
    deliver_at        timestamptz NOT NULL,
    status            text        NOT NULL DEFAULT 'pending',
    delivered_at      timestamptz,
    claimed_at        timestamptz,
    source_message_id bigint,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_time_capsules PRIMARY KEY (id),
    CONSTRAINT uq_time_capsules_user_id_id UNIQUE (user_id, id),       -- target of composite tenant-safe FKs
    CONSTRAINT fk_time_capsules_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_time_capsules_title CHECK (title IS NULL OR civ.is_clean_title(title)),
    CONSTRAINT ck_time_capsules_message CHECK (btrim(message) <> '' AND char_length(message) <= 1000 AND message !~ E'[\\x00-\\x1f\\x7f\\u202a-\\u202e\\u2066-\\u2069]'),
    CONSTRAINT ck_time_capsules_status CHECK (status IN ('pending', 'delivered', 'canceled')),
    CONSTRAINT ck_time_capsules_delivered CHECK ((status = 'delivered') = (delivered_at IS NOT NULL)),
    CONSTRAINT ck_time_capsules_window CHECK (deliver_at >= created_at + interval '6 days' AND deliver_at <= created_at + interval '1830 days')
);
--;;
CREATE UNIQUE INDEX uq_time_capsules_source ON civ.time_capsules (user_id, source_message_id) WHERE source_message_id IS NOT NULL;
--;;
CREATE INDEX ix_time_capsules_user_status_due ON civ.time_capsules (user_id, status, deliver_at);
--;;
-- a capsule that was delivered or canceled is final: it cannot be delivered twice or come back to life
CREATE FUNCTION civ.time_capsules_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'time_capsules: a delivered or canceled capsule is final' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;
--;;
CREATE TRIGGER time_capsules_guard BEFORE UPDATE ON civ.time_capsules FOR EACH ROW EXECUTE FUNCTION civ.time_capsules_guard();
--;;
ALTER TABLE civ.time_capsules ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.time_capsules AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- the words and the date of a letter are never edited, and the application never deletes one: only the life of the capsule moves
GRANT SELECT, INSERT ON civ.time_capsules TO ai_civ_app;
--;;
GRANT UPDATE (status, delivered_at, claimed_at) ON civ.time_capsules TO ai_civ_app;
--;;

-- Whom the hourly job must visit: tenants (active group, onboarding done) with a pending capsule that is due and not under a live lease. Returns tenant ids and
-- nothing else; the quiet hours and the claim are the worker's.
CREATE FUNCTION civ.time_capsules_due_tenants(p_limit integer, p_lease interval)
    RETURNS TABLE (tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
    SELECT DISTINCT c.user_id
    FROM civ.time_capsules c
    JOIN civ.users u ON u.id = c.user_id
    WHERE c.status = 'pending' AND c.deliver_at <= now()
      AND (c.claimed_at IS NULL OR c.claimed_at < now() - p_lease)
      AND u.group_state = 'active' AND u.onboarding_state = 'done' AND u.forum_chat_id IS NOT NULL
    LIMIT GREATEST(p_limit, 1)
$$;
--;;
REVOKE ALL ON FUNCTION civ.time_capsules_due_tenants(integer, interval) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.time_capsules_due_tenants(integer, interval) TO ai_civ_app;
