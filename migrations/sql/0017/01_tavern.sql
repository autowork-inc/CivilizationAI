-- 0017: the Tavern (v2.0, stage 2, PR 2.2): a feed of friends' characters and friendly "cheers".
--   * civ.tavern_feed()          - the ONLY way to read other tenants' profiles. SECURITY DEFINER, patterned on the Diplomat: the caller is only
--                                  civ.current_tenant_id(); every pair is locked (civ.lock_pair) in ONE global order, both rows of the pair must be
--                                  'accepted'; a field leaves the tenant only if ITS owner switched the matching flag of character_profiles on;
--                                  every card read is written to the owner's diplomat_audit_log (scope profile_public). A pair that does not
--                                  qualify yields NO card (nothing tells "removed" from "blocked" from "never was").
--   * civ.tavern_avatar(contact) - the avatar of one friend: the same checks and audit; NULL for every refusal AND for "no avatar".
--   * civ.tavern_cheers          - one friendly impulse per (sender, friend, sender-local day); written only by civ.send_cheer.
--   * civ.send_cheer(contact, kind) - the cheer + 10 XP 'connection' to BOTH sides, in one transaction, under the usual 100 XP/day cap.
--   * civ.xp_grant               - internal: award_xp for a named user (no EXECUTE for the application: only definer functions call it).

-- the shared "may I look at this friend" check: the owner's id, or NULL for ANY reason (no such handle, not mutual, inactive tenant).
-- takes the pair lock first. Not callable by the application.
CREATE FUNCTION civ.tavern_authorize(p_contact_id uuid) RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me    uuid := civ.current_tenant_id();
    v_owner uuid;
BEGIN
    IF p_contact_id IS NULL THEN
        RAISE EXCEPTION 'tavern: contact id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT c.contact_user_id INTO v_owner FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;   -- only MY row is a handle
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    PERFORM civ.lock_pair(v_me, v_owner);
    IF NOT EXISTS (SELECT 1 FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me AND c.status = 'accepted')
       OR NOT EXISTS (SELECT 1 FROM civ.contacts c WHERE c.user_id = v_owner AND c.contact_user_id = v_me AND c.status = 'accepted')
       OR NOT EXISTS (SELECT 1 FROM civ.users u WHERE u.id = v_owner AND u.onboarding_state = 'done' AND u.group_state = 'active') THEN
        RETURN NULL;
    END IF;
    RETURN v_owner;
END
$$;
--;;

CREATE FUNCTION civ.tavern_feed() RETURNS jsonb
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me       uuid := civ.current_tenant_id();
    v_tz       text;
    v_today    date;
    v_cards    jsonb := '[]'::jsonb;
    r          record;
    v_prof     civ.character_profiles%ROWTYPE;
    v_xp       integer;
    v_mood     civ.shared_mood;
    v_interest jsonb;
    v_level    boolean;
    v_mood_on  boolean;
    v_int_on   boolean;
BEGIN
    SELECT u.timezone INTO v_tz FROM civ.users u WHERE u.id = v_me;
    v_today := (now() AT TIME ZONE v_tz)::date;

    -- at most 50 friends; locked in ONE global order (by the pair's ids), so two feeds that share friends cannot deadlock each other
    FOR r IN
        SELECT s.*
        FROM (
            SELECT c.id, c.contact_user_id AS owner_id, c.alias, c.created_at,
                   least(v_me::text, c.contact_user_id::text) || '.' || greatest(v_me::text, c.contact_user_id::text) AS pair_key
            FROM civ.contacts c
            WHERE c.user_id = v_me AND c.status = 'accepted'
            ORDER BY c.created_at, c.id
            LIMIT 50
        ) s
        ORDER BY s.pair_key
    LOOP
        PERFORM civ.lock_pair(v_me, r.owner_id);
        IF NOT EXISTS (SELECT 1 FROM civ.contacts c WHERE c.id = r.id AND c.user_id = v_me AND c.status = 'accepted')
           OR NOT EXISTS (SELECT 1 FROM civ.contacts c WHERE c.user_id = r.owner_id AND c.contact_user_id = v_me AND c.status = 'accepted')
           OR NOT EXISTS (SELECT 1 FROM civ.users u WHERE u.id = r.owner_id AND u.onboarding_state = 'done' AND u.group_state = 'active') THEN
            CONTINUE;
        END IF;

        SELECT * INTO v_prof FROM civ.character_profiles p WHERE p.user_id = r.owner_id;   -- no row = the defaults
        v_level   := COALESCE(v_prof.show_level, true);
        v_mood_on := COALESCE(v_prof.show_mood, false);
        v_int_on  := COALESCE(v_prof.show_interests, false);

        v_xp := NULL;
        IF v_level THEN
            SELECT COALESCE(sum(l.amount), 0)::integer INTO v_xp FROM civ.xp_ledger l WHERE l.user_id = r.owner_id;
        END IF;

        v_mood := NULL;
        IF v_mood_on THEN   -- the same source as the Diplomat: a state the owner CONFIRMED, in force right now, closed vocabulary only
            SELECT s.mood INTO v_mood
            FROM civ.emotional_states s
            WHERE s.user_id = r.owner_id AND s.source = 'user_confirmed' AND s.mood IS NOT NULL AND s.status = 'active'
              AND s.started_at <= now() AND s.expires_at > now()
            ORDER BY s.priority DESC, s.started_at DESC, s.id
            LIMIT 1;
        END IF;

        v_interest := NULL;
        IF v_int_on THEN   -- current, global preference facts, short one-liners; newest first
            SELECT COALESCE(jsonb_agg(t.value_text), '[]'::jsonb) INTO v_interest
            FROM (
                SELECT f.value_text
                FROM civ.core_facts f
                WHERE f.user_id = r.owner_id AND f.valid_to IS NULL AND f.persona_id IS NULL AND f.category = 'preference'
                  AND f.value_text IS NOT NULL AND char_length(f.value_text) BETWEEN 1 AND 80 AND f.value_text !~ '[\x01-\x1f\x7f]'
                ORDER BY f.valid_from DESC, f.id
                LIMIT 8
            ) t;
        END IF;

        v_cards := v_cards || jsonb_build_array(jsonb_build_object(
            'contact_id', r.id,
            'alias', r.alias,
            'character_name', v_prof.character_name,
            'avatar_hash', CASE WHEN v_prof.avatar_bytes IS NULL THEN NULL ELSE left(encode(sha256(v_prof.avatar_bytes), 'hex'), 16) END,
            'level', CASE WHEN v_level THEN COALESCE(v_xp, 0) / 100 + 1 END,
            'total_xp', v_xp,
            'mood', v_mood::text,
            'mood_shared', v_mood_on,
            'interests', v_interest,
            'open_for_intros', COALESCE(v_prof.open_for_intros, false),
            'connected_since', r.created_at,
            'you_cheered_today', EXISTS (
                SELECT 1 FROM civ.tavern_cheers h WHERE h.user_id = v_me AND h.target_user_id = r.owner_id AND h.local_date = v_today),
            'cheered_you_today', EXISTS (
                SELECT 1 FROM civ.tavern_cheers h
                WHERE h.user_id = r.owner_id AND h.target_user_id = v_me AND (h.created_at AT TIME ZONE v_tz)::date = v_today)
        ));
        INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) VALUES (r.owner_id, v_me, 'profile_public', 'allowed');
    END LOOP;
    RETURN v_cards;
END
$$;
--;;

CREATE FUNCTION civ.tavern_avatar(p_contact_id uuid) RETURNS bytea
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_owner  uuid;
    v_avatar bytea;
BEGIN
    v_owner := civ.tavern_authorize(p_contact_id);
    IF v_owner IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT p.avatar_bytes INTO v_avatar FROM civ.character_profiles p WHERE p.user_id = v_owner;
    IF v_avatar IS NOT NULL THEN
        INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision)
        VALUES (v_owner, civ.current_tenant_id(), 'profile_public', 'allowed');
    END IF;
    RETURN v_avatar;   -- NULL = "no avatar" and every refusal alike
END
$$;
--;;

-- ============ cheers ============
CREATE TABLE civ.tavern_cheers (
    id             uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id        uuid        NOT NULL,                  -- the sender
    target_user_id uuid        NOT NULL,
    kind           text        NOT NULL,
    local_date     date        NOT NULL,                  -- the sender's calendar day
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_tavern_cheers PRIMARY KEY (id),
    CONSTRAINT fk_tavern_cheers_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_tavern_cheers_target FOREIGN KEY (target_user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_tavern_cheers_day UNIQUE (user_id, target_user_id, local_date),      -- one cheer a day to one friend
    CONSTRAINT ck_tavern_cheers_not_self CHECK (user_id <> target_user_id),
    CONSTRAINT ck_tavern_cheers_kind CHECK (kind IN ('focus', 'intellect', 'body', 'connection'))
);
--;;
CREATE INDEX ix_tavern_cheers_target_created ON civ.tavern_cheers (target_user_id, created_at);
--;;
ALTER TABLE civ.tavern_cheers ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.tavern_cheers AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- read-only for the application: a cheer exists only through civ.send_cheer (which also pays the XP)
GRANT SELECT ON civ.tavern_cheers TO ai_civ_app;
--;;

-- award_xp for a NAMED user; no EXECUTE for the application (only SECURITY DEFINER functions of this schema call it)
CREATE FUNCTION civ.xp_grant(p_user uuid, p_category text, p_source_ref text, p_amount integer) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
BEGIN
    IF p_user IS NULL OR p_category NOT IN ('focus', 'intellect', 'body', 'connection') OR p_amount IS NULL OR p_amount NOT BETWEEN 1 AND 100
       OR p_source_ref IS NULL OR btrim(p_source_ref) = '' OR char_length(p_source_ref) > 200 OR p_source_ref ~ '[\x01-\x1f\x7f]' THEN
        RAISE EXCEPTION 'invalid XP award' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('civ.xp_ledger'), hashtext(p_user::text));
    IF EXISTS (SELECT 1 FROM civ.xp_ledger l WHERE l.user_id = p_user AND l.source_ref = p_source_ref) THEN
        RETURN 'duplicate';
    END IF;
    IF civ.xp_on_local_day(p_user, now()) + p_amount > 100 THEN
        RETURN 'daily_cap';
    END IF;
    INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (p_user, p_amount, p_category, p_source_ref);
    RETURN 'awarded';
END
$$;
--;;

-- outcome: sent | already_today | unavailable. 'xp' is the SENDER's own result only (the friend's daily cap is not told to anyone).
CREATE FUNCTION civ.send_cheer(p_contact_id uuid, p_kind text) RETURNS jsonb
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me     uuid := civ.current_tenant_id();
    v_owner  uuid;
    v_tz     text;
    v_cheer  uuid;
    v_mine   text;
    v_theirs text;
BEGIN
    IF p_kind IS NULL OR p_kind NOT IN ('focus', 'intellect', 'body', 'connection') THEN
        RAISE EXCEPTION 'tavern: unknown cheer kind' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_owner := civ.tavern_authorize(p_contact_id);
    IF v_owner IS NULL OR NOT EXISTS (SELECT 1 FROM civ.users u WHERE u.id = v_me AND u.onboarding_state = 'done' AND u.group_state = 'active') THEN
        RETURN jsonb_build_object('outcome', 'unavailable');
    END IF;

    SELECT u.timezone INTO v_tz FROM civ.users u WHERE u.id = v_me;
    INSERT INTO civ.tavern_cheers (user_id, target_user_id, kind, local_date)
    VALUES (v_me, v_owner, p_kind, (now() AT TIME ZONE v_tz)::date)
    ON CONFLICT (user_id, target_user_id, local_date) DO NOTHING
    RETURNING id INTO v_cheer;
    IF v_cheer IS NULL THEN
        RETURN jsonb_build_object('outcome', 'already_today');
    END IF;

    -- both sides earn; the two XP locks are always taken in the same order (smaller id first), so crossing cheers cannot deadlock
    IF v_me < v_owner THEN
        v_mine   := civ.xp_grant(v_me, 'connection', 'cheer.' || v_cheer || '.sent', 10);
        v_theirs := civ.xp_grant(v_owner, 'connection', 'cheer.' || v_cheer || '.received', 10);
    ELSE
        v_theirs := civ.xp_grant(v_owner, 'connection', 'cheer.' || v_cheer || '.received', 10);
        v_mine   := civ.xp_grant(v_me, 'connection', 'cheer.' || v_cheer || '.sent', 10);
    END IF;
    RETURN jsonb_build_object('outcome', 'sent', 'xp', v_mine);
END
$$;
--;;

REVOKE ALL ON FUNCTION civ.tavern_authorize(uuid) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.xp_grant(uuid, text, text, integer) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.tavern_feed() FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.tavern_avatar(uuid) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.send_cheer(uuid, text) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.tavern_feed() TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.tavern_avatar(uuid) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.send_cheer(uuid, text) TO ai_civ_app;
