-- 0005 / 01 - the write path of the social graph: invites, the handshake, the notification outbox.
--
-- Rules this migration enforces (the application role ai_civ_app is NOT trusted to follow them):
--   * civ.contacts can no longer be written by the application directly: no INSERT, no UPDATE. A connection comes into being only
--     through civ.accept_invite (both rows in ONE transaction, under a pair lock). The grantor's row can never be forged.
--   * every SECURITY DEFINER function below learns WHO is calling only from civ.current_tenant_id() - never from an argument.
--     (It raises when there is no tenant context: fail-fast, like every query on a tenant table.)
--   * an invite token never reaches the database: only its SHA-256 (computed by the bot). A leaked table gives no working link.
--   * any function that changes contacts takes civ.lock_pair(a, b) FIRST: the lock is symmetric, so crossing invites A->B and B->A
--     serialise instead of deadlocking.
--   * a grant can only be given to a contact that is 'accepted', and a contact that stops being 'accepted' loses its grants.

-- A name shown to another person (invites, aliases): one line, 1-40 characters, no control and no bidi-override characters.
CREATE FUNCTION civ.is_clean_name(p_name text)
    RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT p_name IS NOT NULL
       AND char_length(p_name) BETWEEN 1 AND 40
       AND p_name = btrim(p_name)
       AND p_name !~ E'[\\x01-\\x1f\\x7f\\u202a-\\u202e\\u2066-\\u2069]'
$$;
--;;

CREATE TABLE civ.contact_invites (
    id           uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL,                  -- the inviter (A): the only tenant that can see the row
    token_hash   bytea       NOT NULL,                  -- SHA-256 of the secret deep-link token; globally unique (looked up cross-tenant)
    display_name text        NOT NULL,                  -- how A is shown to whoever opens the link (chosen by A's bot, shown BEFORE consent)
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    used_at      timestamptz,                           -- single use
    used_by      uuid,
    revoked_at   timestamptz,
    CONSTRAINT pk_contact_invites PRIMARY KEY (id),
    CONSTRAINT fk_contact_invites_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_contact_invites_used_by FOREIGN KEY (used_by) REFERENCES civ.users (id) ON DELETE SET NULL,
    CONSTRAINT uq_contact_invites_token UNIQUE (token_hash),
    CONSTRAINT ck_contact_invites_hash CHECK (octet_length(token_hash) = 32),
    CONSTRAINT ck_contact_invites_name CHECK (civ.is_clean_name(display_name)),
    CONSTRAINT ck_contact_invites_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_contact_invites_used CHECK (used_at IS NOT NULL OR used_by IS NULL)
);
--;;
CREATE INDEX ix_contact_invites_user_open ON civ.contact_invites (user_id, expires_at)
    WHERE used_at IS NULL AND revoked_at IS NULL;
--;;

-- Notifications addressed to a tenant (written by SECURITY DEFINER functions in ANOTHER tenant's transaction: the acceptor's
-- session cannot read the inviter's chat, so the fact "tell A" is stored, durably, in A's own outbox and delivered by a worker).
-- Same life cycle as memory_outbox: claim (FOR UPDATE SKIP LOCKED + lease), fencing, backoff, dead-letter, TTL purge.
CREATE TABLE civ.notification_outbox (
    id              bigint            GENERATED ALWAYS AS IDENTITY,
    user_id         uuid              NOT NULL,         -- the recipient
    kind            text              NOT NULL,
    payload         jsonb             NOT NULL DEFAULT '{}',
    idempotency_key text              NOT NULL,
    status          civ.outbox_status NOT NULL DEFAULT 'pending',
    attempts        smallint          NOT NULL DEFAULT 0,
    max_attempts    smallint          NOT NULL DEFAULT 5,
    locked_until    timestamptz,
    last_error      text,
    created_at      timestamptz       NOT NULL DEFAULT now(),
    processed_at    timestamptz,
    CONSTRAINT pk_notification_outbox PRIMARY KEY (id),
    CONSTRAINT fk_notification_outbox_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_notification_outbox_user_idem UNIQUE (user_id, idempotency_key),
    CONSTRAINT ck_notification_outbox_kind CHECK (kind ~ '^[a-z][a-z0-9_]{1,39}$'),
    CONSTRAINT ck_notification_outbox_attempts CHECK (attempts >= 0 AND max_attempts >= 1),
    CONSTRAINT ck_notification_outbox_terminal CHECK ((status IN ('done', 'dead')) = (processed_at IS NOT NULL))
);
--;;
CREATE INDEX ix_notification_outbox_open ON civ.notification_outbox (created_at)
    WHERE status IN ('pending', 'processing', 'failed');
--;;
CREATE INDEX ix_notification_outbox_done_ttl ON civ.notification_outbox (processed_at) WHERE status = 'done';
--;;

ALTER TABLE civ.contact_invites ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.contact_invites AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
ALTER TABLE civ.notification_outbox ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.notification_outbox AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- Invites are created / revoked / consumed only by the functions below; notifications are created only by them, and the worker
-- (running as the recipient) claims and finishes its own rows.
GRANT SELECT ON civ.contact_invites TO ai_civ_app;
--;;
GRANT SELECT, UPDATE ON civ.notification_outbox TO ai_civ_app;
--;;

-- THE HOLE: contacts were writable by the application, so "accepted" could be self-declared.
REVOKE INSERT, UPDATE ON civ.contacts FROM ai_civ_app;
--;;

-- A grant needs an ACCEPTED contact (the FK only proved that a row exists). A missing contact is left to the FK (23503).
CREATE FUNCTION civ.access_grant_needs_accepted_contact() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = civ, pg_temp
    AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM civ.contacts c
        WHERE c.user_id = NEW.user_id AND c.contact_user_id = NEW.grantee_user_id AND c.status <> 'accepted'
    ) THEN
        RAISE EXCEPTION 'access_grants: the contact is not accepted' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
--;;
CREATE TRIGGER trg_access_grants_accepted_contact
    BEFORE INSERT ON civ.access_grants
    FOR EACH ROW EXECUTE FUNCTION civ.access_grant_needs_accepted_contact();
--;;
-- A contact that stops being accepted (blocked, ...) loses its active grants in the same statement.
CREATE FUNCTION civ.contact_left_accepted_revokes_grants() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = civ, pg_temp
    AS $$
BEGIN
    UPDATE civ.access_grants g SET revoked_at = now()
    WHERE g.user_id = NEW.user_id AND g.grantee_user_id = NEW.contact_user_id AND g.revoked_at IS NULL;
    RETURN NULL;
END
$$;
--;;
CREATE TRIGGER trg_contacts_status_revokes_grants
    AFTER UPDATE OF status ON civ.contacts
    FOR EACH ROW WHEN (OLD.status = 'accepted' AND NEW.status <> 'accepted')
    EXECUTE FUNCTION civ.contact_left_accepted_revokes_grants();
--;;

-- ============ functions ============

-- Symmetric lock of a pair of tenants: lock_pair(a, b) and lock_pair(b, a) are the same lock.
CREATE FUNCTION civ.lock_pair(p_a uuid, p_b uuid) RETURNS void
    LANGUAGE plpgsql VOLATILE
    SET search_path = civ, pg_temp
    AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('civ.pair.' || least(p_a::text, p_b::text) || '.' || greatest(p_a::text, p_b::text), 0));
END
$$;
--;;
REVOKE ALL ON FUNCTION civ.lock_pair(uuid, uuid) FROM PUBLIC;
--;;

-- create_invite -> out_outcome: created | not_ready (onboarding not done / group not active) | too_many (5 open invites at most)
CREATE FUNCTION civ.create_invite(p_token_hash bytea, p_display_name text, p_ttl interval DEFAULT interval '72 hours')
    RETURNS TABLE (out_outcome text, out_invite_id uuid, out_expires_at timestamptz)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me     uuid := civ.current_tenant_id();
    v_onb    text;
    v_group  text;
    v_open   integer;
    v_id     uuid;
    v_expiry timestamptz;
BEGIN
    IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 THEN
        RAISE EXCEPTION 'create_invite: token_hash must be a SHA-256 digest' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF NOT civ.is_clean_name(p_display_name) THEN
        RAISE EXCEPTION 'create_invite: display_name must be one clean line of 1-40 characters' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_ttl IS NULL OR p_ttl < interval '1 minute' OR p_ttl > interval '30 days' THEN
        RAISE EXCEPTION 'create_invite: ttl must be between 1 minute and 30 days' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('civ.create_invite.' || v_me::text, 0));  -- the cap below cannot be raced past
    SELECT u.onboarding_state, u.group_state INTO v_onb, v_group FROM civ.users u WHERE u.id = v_me;
    IF NOT FOUND OR v_onb <> 'done' OR v_group <> 'active' THEN
        RETURN QUERY SELECT 'not_ready'::text, NULL::uuid, NULL::timestamptz;
        RETURN;
    END IF;
    SELECT count(*) INTO v_open FROM civ.contact_invites i
    WHERE i.user_id = v_me AND i.used_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now();
    IF v_open >= 5 THEN
        RETURN QUERY SELECT 'too_many'::text, NULL::uuid, NULL::timestamptz;
        RETURN;
    END IF;
    INSERT INTO civ.contact_invites (user_id, token_hash, display_name, expires_at)
    VALUES (v_me, p_token_hash, p_display_name, now() + p_ttl)
    RETURNING id, expires_at INTO v_id, v_expiry;
    RETURN QUERY SELECT 'created'::text, v_id, v_expiry;
END
$$;
--;;

-- Revoke every open invite of the caller; returns how many.
CREATE FUNCTION civ.revoke_invites() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me uuid := civ.current_tenant_id();
    v_n  integer;
BEGIN
    UPDATE civ.contact_invites i SET revoked_at = now()
    WHERE i.user_id = v_me AND i.used_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now();
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$$;
--;;

-- peek_invite: what the confirmation screen needs, and nothing that changes anything. out_state:
--   ok | own (the caller's own link) | already_connected | not_ready (the caller has not finished onboarding) |
--   invalid (unknown, used, revoked, expired - and "the inviter blocked you": all indistinguishable on purpose)
CREATE FUNCTION civ.peek_invite(p_token_hash bytea)
    RETURNS TABLE (out_state text, out_invite_id uuid, out_inviter_name text)
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me     uuid := civ.current_tenant_id();
    v_onb    text;
    v_inv    civ.contact_invites%ROWTYPE;
    v_mine   text;
    v_theirs text;
BEGIN
    IF p_token_hash IS NULL OR octet_length(p_token_hash) <> 32 THEN
        RAISE EXCEPTION 'peek_invite: token_hash must be a SHA-256 digest' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT u.onboarding_state INTO v_onb FROM civ.users u WHERE u.id = v_me;
    IF NOT FOUND OR v_onb <> 'done' THEN
        RETURN QUERY SELECT 'not_ready'::text, NULL::uuid, NULL::text;
        RETURN;
    END IF;
    SELECT * INTO v_inv FROM civ.contact_invites i WHERE i.token_hash = p_token_hash;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::text;
        RETURN;
    END IF;
    IF v_inv.user_id = v_me THEN
        RETURN QUERY SELECT 'own'::text, v_inv.id, v_inv.display_name;
        RETURN;
    END IF;
    SELECT c.status::text INTO v_mine FROM civ.contacts c WHERE c.user_id = v_me AND c.contact_user_id = v_inv.user_id;
    SELECT c.status::text INTO v_theirs FROM civ.contacts c WHERE c.user_id = v_inv.user_id AND c.contact_user_id = v_me;
    IF v_mine = 'accepted' AND v_theirs = 'accepted' THEN
        RETURN QUERY SELECT 'already_connected'::text, v_inv.id, v_inv.display_name;
        RETURN;
    END IF;
    IF v_inv.used_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL OR v_inv.expires_at <= now() OR v_theirs = 'blocked' THEN
        RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::text;
        RETURN;
    END IF;
    RETURN QUERY SELECT 'ok'::text, v_inv.id, v_inv.display_name;
END
$$;
--;;

-- accept_invite: the handshake. The caller (B) accepts the invite of A, introducing themself as p_display_name.
-- out_outcome: connected | already_connected (idempotent: a double click, or a second link) | invalid | own | not_ready.
-- ONE transaction: pair lock -> invite row lock -> both contact rows -> invite consumed -> notification for A.
-- A blocked pair is never silently re-opened by the INVITER's side: if A blocked B the link is simply invalid for B;
-- if B had blocked A, B's own acceptance is the explicit decision that lifts it.
CREATE FUNCTION civ.accept_invite(p_invite_id uuid, p_display_name text)
    RETURNS TABLE (out_outcome text, out_contact_id uuid, out_inviter_name text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me       uuid := civ.current_tenant_id();
    v_onb      text;
    v_inviter  uuid;
    v_inv      civ.contact_invites%ROWTYPE;
    v_my_id    uuid;
    v_mine     text;
    v_their_id uuid;
    v_theirs   text;
BEGIN
    IF p_invite_id IS NULL THEN
        RAISE EXCEPTION 'accept_invite: invite id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF NOT civ.is_clean_name(p_display_name) THEN
        RAISE EXCEPTION 'accept_invite: display_name must be one clean line of 1-40 characters' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT u.onboarding_state INTO v_onb FROM civ.users u WHERE u.id = v_me;
    IF NOT FOUND OR v_onb <> 'done' THEN
        RETURN QUERY SELECT 'not_ready'::text, NULL::uuid, NULL::text;
        RETURN;
    END IF;

    SELECT i.user_id INTO v_inviter FROM civ.contact_invites i WHERE i.id = p_invite_id;   -- who to lock with (no lock yet)
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::text;
        RETURN;
    END IF;
    IF v_inviter = v_me THEN
        RETURN QUERY SELECT 'own'::text, NULL::uuid, NULL::text;
        RETURN;
    END IF;

    PERFORM civ.lock_pair(v_me, v_inviter);                                  -- 1. the pair, always first
    SELECT * INTO v_inv FROM civ.contact_invites i WHERE i.id = p_invite_id FOR UPDATE;   -- 2. then the invite
    SELECT c.id, c.status::text INTO v_my_id, v_mine FROM civ.contacts c
        WHERE c.user_id = v_me AND c.contact_user_id = v_inviter FOR UPDATE;
    SELECT c.id, c.status::text INTO v_their_id, v_theirs FROM civ.contacts c
        WHERE c.user_id = v_inviter AND c.contact_user_id = v_me FOR UPDATE;

    IF v_mine = 'accepted' AND v_theirs = 'accepted' THEN
        RETURN QUERY SELECT 'already_connected'::text, v_my_id, v_inv.display_name;
        RETURN;
    END IF;
    IF v_inv.used_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL OR v_inv.expires_at <= now() OR v_theirs = 'blocked' THEN
        RETURN QUERY SELECT 'invalid'::text, NULL::uuid, NULL::text;
        RETURN;
    END IF;

    INSERT INTO civ.contacts (user_id, contact_user_id, alias, status)
    VALUES (v_me, v_inviter, v_inv.display_name, 'accepted')
    ON CONFLICT (user_id, contact_user_id)
        DO UPDATE SET status = 'accepted', alias = COALESCE(civ.contacts.alias, EXCLUDED.alias)
    RETURNING id INTO v_my_id;

    INSERT INTO civ.contacts (user_id, contact_user_id, alias, status)
    VALUES (v_inviter, v_me, p_display_name, 'accepted')
    ON CONFLICT (user_id, contact_user_id)
        DO UPDATE SET status = 'accepted', alias = COALESCE(civ.contacts.alias, EXCLUDED.alias)
        WHERE civ.contacts.status = 'pending';
    SELECT c.id INTO v_their_id FROM civ.contacts c WHERE c.user_id = v_inviter AND c.contact_user_id = v_me;

    UPDATE civ.contact_invites i SET used_at = now(), used_by = v_me WHERE i.id = p_invite_id;

    INSERT INTO civ.notification_outbox (user_id, kind, payload, idempotency_key)
    VALUES (v_inviter, 'contact_accepted', jsonb_build_object('contact_id', v_their_id), 'contact_accepted.' || p_invite_id::text)
    ON CONFLICT (user_id, idempotency_key) DO NOTHING;

    RETURN QUERY SELECT 'connected'::text, v_my_id, v_inv.display_name;
END
$$;
--;;

-- Which tenants have notifications that can be delivered right now (a group that is active, onboarding done). One indexed query,
-- not a scan of every tenant: the dispatcher visits only tenants with work. Returns tenant ids and nothing else.
CREATE FUNCTION civ.pending_notification_tenants(p_limit integer)
    RETURNS TABLE (tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
    SELECT DISTINCT n.user_id
    FROM civ.notification_outbox n
    JOIN civ.users u ON u.id = n.user_id
    WHERE n.status IN ('pending', 'processing', 'failed')
      AND n.attempts < n.max_attempts
      AND (n.locked_until IS NULL OR n.locked_until < now())
      AND u.group_state = 'active' AND u.onboarding_state = 'done' AND u.forum_chat_id IS NOT NULL
    LIMIT GREATEST(p_limit, 1)
$$;
--;;

-- Housekeeping, one statement per kind for ALL tenants (no per-tenant loop):
--   reaped   - open notifications out of attempts whose lease is over -> dead (a worker may have crashed on the last try);
--   expired  - open notifications older than p_notification_max_age_days -> dead (the group never became deliverable);
--   purged   - done notifications older than p_done_ttl_days are deleted;
--   invites  - invites that ended (used / revoked / expired) more than p_invite_ttl_days ago are deleted.
CREATE FUNCTION civ.purge_social(p_done_ttl_days integer, p_notification_max_age_days integer, p_invite_ttl_days integer)
    RETURNS TABLE (out_reaped bigint, out_expired bigint, out_purged bigint, out_invites bigint)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_reaped  bigint;
    v_expired bigint;
    v_purged  bigint;
    v_invites bigint;
BEGIN
    UPDATE civ.notification_outbox n
    SET status = 'dead', processed_at = now(), locked_until = NULL,
        last_error = COALESCE(n.last_error, 'worker crashed or lease expired on the last attempt')
    WHERE n.status IN ('pending', 'processing', 'failed') AND n.attempts >= n.max_attempts
      AND (n.locked_until IS NULL OR n.locked_until < now());
    GET DIAGNOSTICS v_reaped = ROW_COUNT;

    UPDATE civ.notification_outbox n
    SET status = 'dead', processed_at = now(), locked_until = NULL,
        last_error = COALESCE(n.last_error, 'not delivered within the maximum age')
    WHERE n.status IN ('pending', 'processing', 'failed')
      AND n.created_at < now() - make_interval(days => p_notification_max_age_days)
      AND (n.locked_until IS NULL OR n.locked_until < now());
    GET DIAGNOSTICS v_expired = ROW_COUNT;

    DELETE FROM civ.notification_outbox n
    WHERE n.status = 'done' AND n.processed_at < now() - make_interval(days => p_done_ttl_days);
    GET DIAGNOSTICS v_purged = ROW_COUNT;

    DELETE FROM civ.contact_invites i
    WHERE COALESCE(i.used_at, i.revoked_at, i.expires_at) < now() - make_interval(days => p_invite_ttl_days);
    GET DIAGNOSTICS v_invites = ROW_COUNT;

    RETURN QUERY SELECT v_reaped, v_expired, v_purged, v_invites;
END
$$;
--;;

REVOKE ALL ON FUNCTION civ.create_invite(bytea, text, interval) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.revoke_invites() FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.peek_invite(bytea) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.accept_invite(uuid, text) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.pending_notification_tenants(integer) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.purge_social(integer, integer, integer) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.create_invite(bytea, text, interval) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.revoke_invites() TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.peek_invite(bytea) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.accept_invite(uuid, text) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.pending_notification_tenants(integer) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.purge_social(integer, integer, integer) TO ai_civ_app;
--;;

-- Self-check (like 0001): the migration fails if a table of the schema was left without row level security.
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
