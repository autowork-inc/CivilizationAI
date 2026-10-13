-- 0007 / 01 - the Diplomat (Module 5, step 4): what one person's avatar may learn about another, and the audit of every ask.
--
-- Contract (from 0001/05, 0005, 0006 - the application role is NOT trusted to follow it, the database enforces it):
--   * Cross-tenant reads happen ONLY here, in SECURITY DEFINER functions with search_path = civ, pg_temp, EXECUTE for ai_civ_app only.
--     WHO asks comes only from civ.current_tenant_id() - never from an argument. A contact id is only a handle to the CALLER's own row.
--   * Access = BOTH rows of the pair are 'accepted' (the application may DELETE its own contact row, which leaves a one-sided pair),
--     AND the owner granted this exact scope to the caller (not revoked, not expired), AND the owner is a live tenant (onboarding done,
--     group active: nobody answers on behalf of a frozen or abandoned group).
--   * Every ask that gets as far as "a real, mutual contact asked for something" writes diplomat_audit_log IN THE SAME TRANSACTION
--     (allowed / denied). A refusal is a RETURN VALUE, never an exception: a rollback would destroy the record of the refusal.
--   * The caller can NOT tell why an ask failed: unknown contact, one-sided pair, blocked, no grant, revoked, expired, rate limit,
--     inactive owner, unsupported scope all answer the same {"decision":"unavailable"}.
--   * The pair lock (civ.lock_pair, always first) serialises an ask with block / remove / set_grant of the same pair: a grant that was
--     revoked before an ask can never be honoured by it, and the rate limit cannot be beaten by parallel asks.
--   * Rate limit: 30 asks per (owner, requester, scope) per hour, counted from the audit log itself (allowed AND denied rows count, and a
--     limited ask writes nothing, so a spammer can neither exceed the limit nor flood the owner's log).
--
-- What leaves the database is the minimum: busy intervals WITHOUT titles / descriptions; 'private' events do not exist for the asker;
-- a mood only from a state the owner CONFIRMED and only from the closed vocabulary civ.shared_mood. No uuid of another tenant is returned.

-- ============ Closed vocabulary of what an avatar may say about its owner's mood ============
CREATE TYPE civ.shared_mood AS ENUM ('energetic', 'good', 'calm', 'tired', 'stressed', 'overwhelmed', 'sad', 'irritated');
--;;

-- The shared mood travels with the proposal (the user sees it on the review card and decides) into the confirmed state.
ALTER TABLE civ.pending_facts ADD COLUMN emotion_mood civ.shared_mood;
--;;
ALTER TABLE civ.pending_facts
    ADD CONSTRAINT ck_pending_facts_mood_subject CHECK (emotion_mood IS NULL OR subject = 'emotional_state');
--;;
ALTER TABLE civ.emotional_states ADD COLUMN mood civ.shared_mood;
--;;
-- Only a state the USER confirmed may carry a mood that others can be told about (an AI inference never leaves the tenant).
ALTER TABLE civ.emotional_states
    ADD CONSTRAINT ck_emotional_states_mood_confirmed CHECK (mood IS NULL OR source = 'user_confirmed');
--;;
-- ...and it must be exactly the mood of the proposal the user approved.
CREATE OR REPLACE FUNCTION civ.emotional_states_guard_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.source = 'user_confirmed' AND NOT EXISTS (
        SELECT 1 FROM civ.pending_facts p
        WHERE p.id = NEW.source_pending_id AND p.user_id = NEW.user_id
          AND p.subject = 'emotional_state' AND p.status = 'approved'
          AND p.emotion_mood IS NOT DISTINCT FROM NEW.mood
    ) THEN
        RAISE EXCEPTION 'emotional_states: source=user_confirmed requires an APPROVED emotional_state pending_facts row with the same mood'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
--;;

-- What a Diplomat answer put into a conversation: another person's data. The nightly GC must never turn it into the asker's memory
-- (it would outlive the grant), so the reply is flagged and the GC skips flagged rows.
ALTER TABLE civ.messages ADD COLUMN third_party_data boolean NOT NULL DEFAULT false;
--;;

-- Rate limit and "who asked me" lookups.
CREATE INDEX ix_diplomat_audit_log_pair ON civ.diplomat_audit_log (user_id, requester_user_id, scope, created_at);
--;;

-- ============ The shared gate: every Diplomat function goes through it ============
-- Returns ONE row (the owner and the constraints of the grant) when access is allowed, NO row when it is not. It also writes the audit
-- row. Not callable by the application (it is invoked by the public functions below, which run with the owner's rights).
CREATE FUNCTION civ.diplomat_authorize(p_contact_id uuid, p_scope civ.acl_scope)
    RETURNS TABLE (o_owner uuid, o_constraints jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me          uuid := civ.current_tenant_id();
    v_owner       uuid;
    v_status      civ.contact_status;
    v_constraints jsonb;
BEGIN
    IF p_contact_id IS NULL THEN
        RAISE EXCEPTION 'diplomat: contact id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT c.contact_user_id INTO v_owner FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;   -- only MY row is a handle
    IF NOT FOUND THEN
        RETURN;
    END IF;

    PERFORM civ.lock_pair(v_me, v_owner);   -- first, like every function that touches a pair

    SELECT c.status INTO v_status FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;   -- re-read under the lock
    IF NOT FOUND OR v_status <> 'accepted' THEN
        RETURN;
    END IF;
    IF NOT EXISTS (   -- the OTHER row of the pair must be accepted too; a one-sided pair (removed / blocked us) gets nothing, and no audit row
        SELECT 1 FROM civ.contacts c WHERE c.user_id = v_owner AND c.contact_user_id = v_me AND c.status = 'accepted'
    ) THEN
        RETURN;
    END IF;
    IF NOT EXISTS (   -- nobody answers for a tenant who has not finished onboarding, or whose group is frozen / abandoned
        SELECT 1 FROM civ.users u WHERE u.id = v_owner AND u.onboarding_state = 'done' AND u.group_state = 'active'
    ) THEN
        RETURN;
    END IF;

    IF (
        SELECT count(*) FROM civ.diplomat_audit_log l
        WHERE l.user_id = v_owner AND l.requester_user_id = v_me AND l.scope = p_scope AND l.created_at > now() - interval '1 hour'
    ) >= 30 THEN
        RETURN;
    END IF;

    SELECT g.constraints INTO v_constraints
    FROM civ.access_grants g
    WHERE g.user_id = v_owner AND g.grantee_user_id = v_me AND g.scope = p_scope
      AND g.revoked_at IS NULL AND (g.expires_at IS NULL OR g.expires_at > now());
    IF NOT FOUND THEN
        INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) VALUES (v_owner, v_me, p_scope, 'denied');
        RETURN;
    END IF;

    INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) VALUES (v_owner, v_me, p_scope, 'allowed');
    o_owner := v_owner;
    o_constraints := v_constraints;
    RETURN NEXT;
END
$$;
--;;

-- ============ calendar_free_busy ============
-- Busy time of the contact inside [p_from, p_to), clipped to "now .. now + horizon" (grant constraint horizon_days, default 7, at most 14).
-- Recurring series are NOT expanded here (RRULE expansion lives in the application); the function returns the rows the expansion needs
-- and nothing else: no titles, no descriptions, no ids. Event kinds in the answer:
--   single    {starts_at, ends_at}
--   series    {ref, starts_at, ends_at, timezone, rule}             (ref = an opaque number, valid inside this answer only)
--   exception {series_ref, original_start, suppress, starts_at?, ends_at?}   (suppress = this occurrence is gone: cancelled or made private)
-- 'private' events do not exist for the asker; cancelled / rescheduled ones do not occupy time.
CREATE FUNCTION civ.diplomat_free_busy(p_contact_id uuid, p_from timestamptz, p_to timestamptz)
    RETURNS jsonb
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_owner       uuid;
    v_constraints jsonb;
    v_horizon     integer := 7;
    v_from        timestamptz;
    v_to          timestamptz;
    v_events      jsonb;
BEGIN
    IF p_contact_id IS NULL OR p_from IS NULL OR p_to IS NULL OR p_to <= p_from THEN
        RAISE EXCEPTION 'diplomat_free_busy: contact id and a non-empty period are required' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT a.o_owner, a.o_constraints INTO v_owner, v_constraints FROM civ.diplomat_authorize(p_contact_id, 'calendar_free_busy') a;
    IF v_owner IS NULL THEN
        RETURN jsonb_build_object('decision', 'unavailable');
    END IF;

    IF jsonb_typeof(v_constraints -> 'horizon_days') = 'number' THEN
        v_horizon := LEAST(GREATEST(floor((v_constraints ->> 'horizon_days')::numeric), 1), 14)::integer;
    END IF;
    v_from := GREATEST(p_from, now());
    v_to   := LEAST(p_to, now() + make_interval(days => v_horizon));
    IF v_from >= v_to THEN
        RETURN jsonb_build_object(
            'decision', 'allowed',
            'window', jsonb_build_object('from', v_from, 'to', v_from, 'horizon_days', v_horizon),
            'events', '[]'::jsonb);
    END IF;

    WITH series AS (
        SELECT e.id, e.starts_at, e.ends_at, e.timezone, e.recurrence_rule,
               row_number() OVER (ORDER BY e.id) AS ref
        FROM civ.events e
        WHERE e.user_id = v_owner AND e.parent_event_id IS NULL AND e.recurrence_rule IS NOT NULL
          AND e.visibility <> 'private' AND e.status NOT IN ('cancelled', 'rescheduled')
    ),
    items AS (
        SELECT jsonb_build_object('kind', 'single', 'starts_at', e.starts_at, 'ends_at', e.ends_at) AS item
        FROM civ.events e
        WHERE e.user_id = v_owner AND e.parent_event_id IS NULL AND e.recurrence_rule IS NULL
          AND e.visibility <> 'private' AND e.status NOT IN ('cancelled', 'rescheduled')
          AND e.starts_at < v_to AND e.ends_at > v_from
        UNION ALL
        SELECT jsonb_build_object('kind', 'series', 'ref', s.ref, 'starts_at', s.starts_at, 'ends_at', s.ends_at,
                                  'timezone', s.timezone, 'rule', s.recurrence_rule)
        FROM series s
        WHERE s.starts_at < v_to
        UNION ALL
        SELECT jsonb_build_object(
                   'kind', 'exception', 'series_ref', s.ref, 'original_start', e.original_start, 'suppress', x.suppress,
                   'starts_at', CASE WHEN x.suppress THEN NULL ELSE e.starts_at END,
                   'ends_at',   CASE WHEN x.suppress THEN NULL ELSE e.ends_at END)
        FROM civ.events e
        JOIN series s ON s.id = e.parent_event_id
        CROSS JOIN LATERAL (SELECT (e.status IN ('cancelled', 'rescheduled') OR e.visibility = 'private') AS suppress) x
        WHERE e.user_id = v_owner
          AND ((e.original_start < v_to AND e.original_start + (s.ends_at - s.starts_at) > v_from)
               OR (NOT x.suppress AND e.starts_at < v_to AND e.ends_at > v_from))
        UNION ALL
        -- a visible exception of a PRIVATE series is just an event of its own (the series itself stays invisible)
        SELECT jsonb_build_object('kind', 'single', 'starts_at', e.starts_at, 'ends_at', e.ends_at)
        FROM civ.events e
        JOIN civ.events m ON m.user_id = e.user_id AND m.id = e.parent_event_id
        WHERE e.user_id = v_owner AND m.visibility = 'private' AND m.status NOT IN ('cancelled', 'rescheduled')
          AND e.visibility <> 'private' AND e.status NOT IN ('cancelled', 'rescheduled')
          AND e.starts_at < v_to AND e.ends_at > v_from
    )
    SELECT COALESCE(jsonb_agg(i.item), '[]'::jsonb) INTO v_events FROM items i;

    RETURN jsonb_build_object(
        'decision', 'allowed',
        'window', jsonb_build_object('from', v_from, 'to', v_to, 'horizon_days', v_horizon),
        'events', v_events);
END
$$;
--;;

-- ============ emotional_state_label ============
-- The contact's current mood from the CLOSED vocabulary, only from a state the contact confirmed and that is in force right now.
-- decision = allowed with a JSON null mood means nothing is confirmed at the moment (the asker must not read it as "fine").
CREATE FUNCTION civ.diplomat_mood(p_contact_id uuid)
    RETURNS jsonb
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_owner uuid;
    v_mood  civ.shared_mood;
BEGIN
    IF p_contact_id IS NULL THEN
        RAISE EXCEPTION 'diplomat_mood: contact id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT a.o_owner INTO v_owner FROM civ.diplomat_authorize(p_contact_id, 'emotional_state_label') a;
    IF v_owner IS NULL THEN
        RETURN jsonb_build_object('decision', 'unavailable');
    END IF;

    SELECT s.mood INTO v_mood
    FROM civ.emotional_states s
    WHERE s.user_id = v_owner AND s.source = 'user_confirmed' AND s.mood IS NOT NULL AND s.status = 'active'
      AND s.started_at <= now() AND s.expires_at > now()
    ORDER BY s.priority DESC, s.started_at DESC, s.id
    LIMIT 1;

    RETURN jsonb_build_object('decision', 'allowed', 'mood', v_mood::text);
END
$$;
--;;

REVOKE ALL ON FUNCTION civ.diplomat_authorize(uuid, civ.acl_scope) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.diplomat_free_busy(uuid, timestamptz, timestamptz) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.diplomat_mood(uuid) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.diplomat_free_busy(uuid, timestamptz, timestamptz) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.diplomat_mood(uuid) TO ai_civ_app;
