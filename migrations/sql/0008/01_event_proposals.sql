-- 0008 / 01 - events from conversation (Module 5, step 5): a proposal from one person to another, and a draft the user confirms.
--
-- Two different things, deliberately two tables:
--   * civ.event_proposals - B asks A to a meeting. It crosses tenants, so the application role can NOT write it: it comes into being
--     only through civ.propose_event and is decided only through civ.respond_event_proposal (SECURITY DEFINER, WHO is calling comes only
--     from civ.current_tenant_id(), never from an argument). Both parties can READ the row (their own proposals / proposals to them).
--     Nothing is written to anybody's calendar until the recipient presses "accept": then BOTH calendars get a row in ONE transaction
--     (created_via = 'peer_proposal', which keeps meetings apart from personal plans).
--   * civ.event_drafts - "put it into my calendar?" for the user's OWN plans heard in conversation ("I am going for 2 hours"). A plain
--     tenant table (own rows only, normal RLS): the bot writes the draft, the user's button turns it into an events row.
--
-- Rules the database enforces (the application role is NOT trusted to follow them):
--   * a proposal needs a MUTUAL contact (both rows 'accepted', checked under civ.lock_pair, first) and two live tenants (onboarding done,
--     group active). Every reason for refusal answers the same 'unavailable', so the asker cannot tell a blocked pair from an unknown contact.
--   * limits, counted from the table itself under a per-proposer lock: 5 open per pair, 10 per pair and 30 per proposer in 24 hours.
--   * a replayed call (same pair, title, start while still open) returns the open proposal instead of a second one (idempotent).
--   * the title is one clean line (civ.is_clean_title): it is shown to another person.
--   * a decision is final and idempotent: a second click answers what was decided, it never creates a second event.

-- A text shown to another person (an event title): one line, 1-80 characters, no control and no bidi-override characters.
CREATE FUNCTION civ.is_clean_title(p_title text)
    RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT p_title IS NOT NULL
       AND char_length(p_title) BETWEEN 1 AND 80
       AND p_title = btrim(p_title)
       AND p_title !~ E'[\\x01-\\x1f\\x7f\\u202a-\\u202e\\u2066-\\u2069]'
$$;
--;;

-- A meeting between two people is not a personal plan: the source says which it was.
ALTER TABLE civ.events DROP CONSTRAINT ck_events_created_via;
--;;
ALTER TABLE civ.events
    ADD CONSTRAINT ck_events_created_via CHECK (created_via IN ('mini_app', 'bot_confirmed', 'peer_proposal'));
--;;

CREATE TABLE civ.event_proposals (
    id                 uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id            uuid        NOT NULL,                  -- the PROPOSER (the tenant who owns the row)
    recipient_user_id  uuid        NOT NULL,                  -- who is asked
    title              text        NOT NULL,
    starts_at          timestamptz NOT NULL,
    ends_at            timestamptz NOT NULL,
    status             text        NOT NULL DEFAULT 'pending',
    created_at         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL,                  -- the earlier of the start and 48 hours after the proposal
    decided_at         timestamptz,                           -- set by the answer, or by expiry
    proposer_event_id  uuid,                                  -- the rows created on acceptance (SET NULL if the user deletes the event)
    recipient_event_id uuid,
    CONSTRAINT pk_event_proposals PRIMARY KEY (id),
    CONSTRAINT fk_event_proposals_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_event_proposals_recipient FOREIGN KEY (recipient_user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_event_proposals_proposer_event FOREIGN KEY (proposer_event_id) REFERENCES civ.events (id) ON DELETE SET NULL,
    CONSTRAINT fk_event_proposals_recipient_event FOREIGN KEY (recipient_event_id) REFERENCES civ.events (id) ON DELETE SET NULL,
    CONSTRAINT ck_event_proposals_parties CHECK (user_id <> recipient_user_id),
    CONSTRAINT ck_event_proposals_title CHECK (civ.is_clean_title(title)),
    CONSTRAINT ck_event_proposals_period CHECK (ends_at > starts_at AND ends_at - starts_at <= interval '1 day'),
    CONSTRAINT ck_event_proposals_status CHECK (status IN ('pending', 'accepted', 'declined', 'expired')),
    CONSTRAINT ck_event_proposals_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_event_proposals_decided CHECK ((status = 'pending') = (decided_at IS NULL)),
    CONSTRAINT ck_event_proposals_events CHECK (status = 'accepted' OR (proposer_event_id IS NULL AND recipient_event_id IS NULL))
);
--;;
-- an open proposal is unique per (pair, title, start): a replay finds it instead of adding a second one
CREATE UNIQUE INDEX uq_event_proposals_pending ON civ.event_proposals (user_id, recipient_user_id, title, starts_at)
    WHERE status = 'pending';
--;;
CREATE INDEX ix_event_proposals_recipient ON civ.event_proposals (recipient_user_id, created_at);
--;;
CREATE INDEX ix_event_proposals_pair_created ON civ.event_proposals (user_id, recipient_user_id, created_at);
--;;
CREATE INDEX ix_event_proposals_open_expiry ON civ.event_proposals (expires_at) WHERE status = 'pending';
--;;

CREATE TABLE civ.event_drafts (
    id          uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id     uuid        NOT NULL,
    persona_id  uuid,                                          -- the topic the plan was heard in (NULL = the main chat)
    title       text        NOT NULL,
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    timezone    text        NOT NULL,                          -- IANA zone of the user when the draft was made; the event keeps it
    status      text        NOT NULL DEFAULT 'pending',
    event_id    uuid,                                          -- the event the confirmation created
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    decided_at  timestamptz,
    CONSTRAINT pk_event_drafts PRIMARY KEY (id),
    CONSTRAINT fk_event_drafts_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_event_drafts_persona FOREIGN KEY (user_id, persona_id)
        REFERENCES civ.personas (user_id, id) ON DELETE SET NULL (persona_id),
    CONSTRAINT fk_event_drafts_event FOREIGN KEY (user_id, event_id)
        REFERENCES civ.events (user_id, id) ON DELETE SET NULL (event_id),
    CONSTRAINT ck_event_drafts_title CHECK (civ.is_clean_title(title)),
    CONSTRAINT ck_event_drafts_period CHECK (ends_at > starts_at AND ends_at - starts_at <= interval '1 day'),
    CONSTRAINT ck_event_drafts_status CHECK (status IN ('pending', 'confirmed', 'dismissed', 'expired')),
    CONSTRAINT ck_event_drafts_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_event_drafts_decided CHECK ((status = 'pending') = (decided_at IS NULL)),
    CONSTRAINT ck_event_drafts_event CHECK (status = 'confirmed' OR event_id IS NULL)
);
--;;
CREATE UNIQUE INDEX uq_event_drafts_pending ON civ.event_drafts (user_id, title, starts_at) WHERE status = 'pending';
--;;
CREATE INDEX ix_event_drafts_user_created ON civ.event_drafts (user_id, created_at);
--;;
CREATE INDEX ix_event_drafts_open_expiry ON civ.event_drafts (expires_at) WHERE status = 'pending';
--;;
CREATE TRIGGER trg_event_drafts_validate_timezone
    BEFORE INSERT OR UPDATE OF timezone ON civ.event_drafts
    FOR EACH ROW EXECUTE FUNCTION civ.validate_timezone();
--;;

ALTER TABLE civ.event_proposals ENABLE ROW LEVEL SECURITY;
--;;
-- Both parties READ (the proposer sees the answer, the recipient sees the card). There is no write policy and no write privilege:
-- the only writers are the SECURITY DEFINER functions below, running as the owner.
CREATE POLICY parties_can_read ON civ.event_proposals AS PERMISSIVE FOR SELECT TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()) OR recipient_user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT ON civ.event_proposals TO ai_civ_app;
--;;
ALTER TABLE civ.event_drafts ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.event_drafts AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.event_drafts TO ai_civ_app;
--;;

-- ============ functions ============

-- propose_event: the CALLER asks one of their contacts to a meeting. The contact id is only a handle to the caller's own row.
-- out_outcome: proposed | already_proposed (the same proposal is still open: its id is returned) | too_many | unavailable
-- 'unavailable' is every other reason (unknown contact, a one-sided pair, blocked, a tenant that is not live) and says nothing about which.
-- Bad input (not a clean title, a start in the past or beyond a year, a period that is empty or longer than a day) raises 22023.
-- ONE transaction: pair lock -> the proposal row -> the notification for the recipient (their own outbox, delivered by a worker).
CREATE FUNCTION civ.propose_event(p_contact_id uuid, p_title text, p_starts_at timestamptz, p_ends_at timestamptz)
    RETURNS TABLE (out_outcome text, out_proposal_id uuid, out_expires_at timestamptz)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me     uuid := civ.current_tenant_id();
    v_to     uuid;
    v_status civ.contact_status;
    v_id     uuid;
    v_expiry timestamptz;
BEGIN
    IF p_contact_id IS NULL THEN
        RAISE EXCEPTION 'propose_event: contact id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF NOT civ.is_clean_title(p_title) THEN
        RAISE EXCEPTION 'propose_event: title must be one clean line of 1-80 characters' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_starts_at IS NULL OR p_ends_at IS NULL OR p_ends_at <= p_starts_at OR p_ends_at - p_starts_at > interval '1 day' THEN
        RAISE EXCEPTION 'propose_event: the period must be non-empty and at most one day' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_starts_at <= now() OR p_starts_at > now() + interval '366 days' THEN
        RAISE EXCEPTION 'propose_event: the start must be in the future, within a year' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT c.contact_user_id INTO v_to FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;   -- only MY row is a handle
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'unavailable'::text, NULL::uuid, NULL::timestamptz;
        RETURN;
    END IF;

    PERFORM civ.lock_pair(v_me, v_to);   -- first, like every function that touches a pair

    SELECT c.status INTO v_status FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;   -- re-read under the lock
    IF NOT FOUND OR v_status <> 'accepted'
       OR NOT EXISTS (SELECT 1 FROM civ.contacts c WHERE c.user_id = v_to AND c.contact_user_id = v_me AND c.status = 'accepted')
       OR (SELECT count(*) FROM civ.users u
           WHERE u.id IN (v_me, v_to) AND u.onboarding_state = 'done' AND u.group_state = 'active') <> 2 THEN
        RETURN QUERY SELECT 'unavailable'::text, NULL::uuid, NULL::timestamptz;
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('civ.propose_event.' || v_me::text, 0));   -- the limits below cannot be raced past

    UPDATE civ.event_proposals p SET status = 'expired', decided_at = now()   -- an open one that ran out of time must not block a new one
    WHERE p.user_id = v_me AND p.recipient_user_id = v_to AND p.status = 'pending' AND p.expires_at <= now();

    SELECT p.id, p.expires_at INTO v_id, v_expiry FROM civ.event_proposals p
    WHERE p.user_id = v_me AND p.recipient_user_id = v_to AND p.title = p_title AND p.starts_at = p_starts_at AND p.status = 'pending';
    IF FOUND THEN
        RETURN QUERY SELECT 'already_proposed'::text, v_id, v_expiry;
        RETURN;
    END IF;

    IF (SELECT count(*) FROM civ.event_proposals p WHERE p.user_id = v_me AND p.recipient_user_id = v_to AND p.status = 'pending') >= 5
       OR (SELECT count(*) FROM civ.event_proposals p
           WHERE p.user_id = v_me AND p.recipient_user_id = v_to AND p.created_at > now() - interval '24 hours') >= 10
       OR (SELECT count(*) FROM civ.event_proposals p WHERE p.user_id = v_me AND p.created_at > now() - interval '24 hours') >= 30 THEN
        RETURN QUERY SELECT 'too_many'::text, NULL::uuid, NULL::timestamptz;
        RETURN;
    END IF;

    v_expiry := LEAST(p_starts_at, now() + interval '48 hours');
    INSERT INTO civ.event_proposals (user_id, recipient_user_id, title, starts_at, ends_at, expires_at)
    VALUES (v_me, v_to, p_title, p_starts_at, p_ends_at, v_expiry)
    RETURNING id INTO v_id;

    INSERT INTO civ.notification_outbox (user_id, kind, payload, idempotency_key)
    VALUES (v_to, 'event_proposal', jsonb_build_object('proposal_id', v_id), 'event_proposal.' || v_id::text)
    ON CONFLICT (user_id, idempotency_key) DO NOTHING;

    RETURN QUERY SELECT 'proposed'::text, v_id, v_expiry;
END
$$;
--;;

-- respond_event_proposal: the RECIPIENT accepts or declines. The proposal id is only a handle to a proposal addressed to the caller.
-- out_outcome: accepted | declined | already_accepted | already_declined | expired | unavailable
-- (unavailable = not addressed to the caller, or the pair is no longer mutual / live; nothing is changed then).
-- ONE transaction: pair lock -> FOR UPDATE of the proposal -> on accept a calendar row for EACH side (each in their own time zone,
-- visibility busy_only, created_via peer_proposal) -> the proposal decided -> the answer for the proposer (their own outbox).
CREATE FUNCTION civ.respond_event_proposal(p_proposal_id uuid, p_accept boolean)
    RETURNS TABLE (out_outcome text, out_event_id uuid)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me       uuid := civ.current_tenant_id();
    v_from     uuid;
    v_p        civ.event_proposals%ROWTYPE;
    v_my_tz    text;
    v_their_tz text;
    v_mine     uuid;
    v_theirs   uuid;
BEGIN
    IF p_proposal_id IS NULL OR p_accept IS NULL THEN
        RAISE EXCEPTION 'respond_event_proposal: proposal id and the answer are required' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT p.user_id INTO v_from FROM civ.event_proposals p WHERE p.id = p_proposal_id AND p.recipient_user_id = v_me;   -- who to lock with
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'unavailable'::text, NULL::uuid;
        RETURN;
    END IF;

    PERFORM civ.lock_pair(v_me, v_from);   -- 1. the pair, always first
    SELECT * INTO v_p FROM civ.event_proposals p WHERE p.id = p_proposal_id FOR UPDATE;   -- 2. then the proposal
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'unavailable'::text, NULL::uuid;
        RETURN;
    END IF;

    IF v_p.status = 'accepted' THEN
        RETURN QUERY SELECT 'already_accepted'::text, v_p.recipient_event_id;
        RETURN;
    ELSIF v_p.status = 'declined' THEN
        RETURN QUERY SELECT 'already_declined'::text, NULL::uuid;
        RETURN;
    ELSIF v_p.status = 'expired' THEN
        RETURN QUERY SELECT 'expired'::text, NULL::uuid;
        RETURN;
    END IF;
    IF v_p.expires_at <= now() THEN
        UPDATE civ.event_proposals p SET status = 'expired', decided_at = now() WHERE p.id = p_proposal_id;
        RETURN QUERY SELECT 'expired'::text, NULL::uuid;
        RETURN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM civ.contacts c WHERE c.user_id = v_me AND c.contact_user_id = v_from AND c.status = 'accepted')
       OR NOT EXISTS (SELECT 1 FROM civ.contacts c WHERE c.user_id = v_from AND c.contact_user_id = v_me AND c.status = 'accepted')
       OR (SELECT count(*) FROM civ.users u
           WHERE u.id IN (v_me, v_from) AND u.onboarding_state = 'done' AND u.group_state = 'active') <> 2 THEN
        RETURN QUERY SELECT 'unavailable'::text, NULL::uuid;
        RETURN;
    END IF;

    IF NOT p_accept THEN
        UPDATE civ.event_proposals p SET status = 'declined', decided_at = now() WHERE p.id = p_proposal_id;
        INSERT INTO civ.notification_outbox (user_id, kind, payload, idempotency_key)
        VALUES (v_from, 'event_proposal_answer', jsonb_build_object('proposal_id', p_proposal_id, 'accepted', false),
                'event_proposal_answer.' || p_proposal_id::text)
        ON CONFLICT (user_id, idempotency_key) DO NOTHING;
        RETURN QUERY SELECT 'declined'::text, NULL::uuid;
        RETURN;
    END IF;

    SELECT u.timezone INTO v_my_tz FROM civ.users u WHERE u.id = v_me;
    SELECT u.timezone INTO v_their_tz FROM civ.users u WHERE u.id = v_from;

    INSERT INTO civ.events (user_id, title, starts_at, ends_at, timezone, visibility, created_via)
    VALUES (v_me, v_p.title, v_p.starts_at, v_p.ends_at, v_my_tz, 'busy_only', 'peer_proposal')
    RETURNING id INTO v_mine;
    INSERT INTO civ.events (user_id, title, starts_at, ends_at, timezone, visibility, created_via)
    VALUES (v_from, v_p.title, v_p.starts_at, v_p.ends_at, v_their_tz, 'busy_only', 'peer_proposal')
    RETURNING id INTO v_theirs;

    UPDATE civ.event_proposals p
    SET status = 'accepted', decided_at = now(), recipient_event_id = v_mine, proposer_event_id = v_theirs
    WHERE p.id = p_proposal_id;

    INSERT INTO civ.notification_outbox (user_id, kind, payload, idempotency_key)
    VALUES (v_from, 'event_proposal_answer', jsonb_build_object('proposal_id', p_proposal_id, 'accepted', true),
            'event_proposal_answer.' || p_proposal_id::text)
    ON CONFLICT (user_id, idempotency_key) DO NOTHING;

    RETURN QUERY SELECT 'accepted'::text, v_mine;
END
$$;
--;;

-- Housekeeping for ALL tenants, one statement per kind (no per-tenant loop):
--   drafts expired    - open drafts past their time -> expired;       drafts purged    - decided drafts older than p_draft_ttl_days are deleted;
--   proposals expired - open proposals past their time -> expired;    proposals purged - decided proposals older than p_proposal_ttl_days are deleted.
-- A notification that points at a purged proposal finds nothing and is simply finished by the worker.
CREATE FUNCTION civ.purge_events(p_draft_ttl_days integer, p_proposal_ttl_days integer)
    RETURNS TABLE (out_drafts_expired bigint, out_drafts_purged bigint, out_proposals_expired bigint, out_proposals_purged bigint)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_de bigint;
    v_dp bigint;
    v_pe bigint;
    v_pp bigint;
BEGIN
    UPDATE civ.event_drafts d SET status = 'expired', decided_at = now() WHERE d.status = 'pending' AND d.expires_at <= now();
    GET DIAGNOSTICS v_de = ROW_COUNT;

    DELETE FROM civ.event_drafts d WHERE d.status <> 'pending' AND d.decided_at < now() - make_interval(days => p_draft_ttl_days);
    GET DIAGNOSTICS v_dp = ROW_COUNT;

    UPDATE civ.event_proposals p SET status = 'expired', decided_at = now() WHERE p.status = 'pending' AND p.expires_at <= now();
    GET DIAGNOSTICS v_pe = ROW_COUNT;

    DELETE FROM civ.event_proposals p
    WHERE p.status <> 'pending' AND p.decided_at < now() - make_interval(days => p_proposal_ttl_days);
    GET DIAGNOSTICS v_pp = ROW_COUNT;

    RETURN QUERY SELECT v_de, v_dp, v_pe, v_pp;
END
$$;
--;;

REVOKE ALL ON FUNCTION civ.propose_event(uuid, text, timestamptz, timestamptz) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.respond_event_proposal(uuid, boolean) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.purge_events(integer, integer) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.propose_event(uuid, text, timestamptz, timestamptz) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.respond_event_proposal(uuid, boolean) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.purge_events(integer, integer) TO ai_civ_app;
--;;

-- Self-check (like 0001 / 0005): the migration fails if a table of the schema was left without row level security.
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
