-- 0023: the decay system (v2.0, stage 5, PR 5.3): negative XP entries for system penalties.
--   * ck_xp_ledger_amount      - was BETWEEN 1 AND 100 (earnings only). Now -100..100; a row with amount <= 0 is allowed only for a system penalty (source_ref decay:* / penalty:*),
--                                and a ZERO row is the "floored" marker of such a penalty: a penalty that could not take anything away (the branch was already at 0) is still
--                                recorded, so that it is not applied later, when the user has earned something, and a re-run stays a no-op.
--   * civ.xp_on_local_day      - the daily cap (100 XP) limits EARNINGS: only positive amounts count, a penalty never re-opens the cap.
--   * xp_ledger_daily_cap      - ignores non-positive inserts.
--   * xp_ledger_floor          - the hard invariant, whoever inserts: no branch (and so no total) can go below 0 (minimum level = 1).
--   * civ.apply_xp_penalty     - the one write path of the decay jobs: idempotent per source_ref, clamps the penalty to what the branch has.
--   * civ.xp_streak_days       - consecutive tenant-local days (ending today, or yesterday if today has no award yet) with at least one earning.

ALTER TABLE civ.xp_ledger DROP CONSTRAINT ck_xp_ledger_amount;
--;;
ALTER TABLE civ.xp_ledger ADD CONSTRAINT ck_xp_ledger_amount CHECK (
    amount BETWEEN -100 AND 100 AND (amount > 0 OR source_ref ~ '^(decay|penalty):')
);
--;;

-- earnings only: a penalty must not make room for more earnings on the same day
CREATE OR REPLACE FUNCTION civ.xp_on_local_day(p_user uuid, p_at timestamptz) RETURNS integer
    LANGUAGE sql STABLE
    AS $$
    SELECT COALESCE(sum(l.amount), 0)::integer
    FROM civ.xp_ledger l
    WHERE l.user_id = p_user
      AND l.amount > 0
      AND (l.created_at AT TIME ZONE (SELECT u.timezone FROM civ.users u WHERE u.id = p_user))::date
        = (p_at AT TIME ZONE (SELECT u.timezone FROM civ.users u WHERE u.id = p_user))::date
$$;
--;;
CREATE OR REPLACE FUNCTION civ.xp_ledger_daily_cap() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.amount <= 0 THEN
        RETURN NEW;  -- penalties and markers are not earnings
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('civ.xp_ledger'), hashtext(NEW.user_id::text));
    IF civ.xp_on_local_day(NEW.user_id, NEW.created_at) + NEW.amount > 100 THEN
        RAISE EXCEPTION 'daily XP cap (100) reached' USING ERRCODE = '23514', CONSTRAINT = 'ck_xp_ledger_daily_cap';
    END IF;
    RETURN NEW;
END
$$;
--;;

-- the floor: a branch never goes below zero (so neither does the total). Same advisory lock as the cap, so a penalty and an award of one tenant serialise.
CREATE FUNCTION civ.xp_ledger_floor() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.amount >= 0 THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('civ.xp_ledger'), hashtext(NEW.user_id::text));
    IF COALESCE((SELECT sum(l.amount) FROM civ.xp_ledger l WHERE l.user_id = NEW.user_id AND l.category = NEW.category), 0) + NEW.amount < 0 THEN
        RAISE EXCEPTION 'XP of a branch cannot go below 0' USING ERRCODE = '23514', CONSTRAINT = 'ck_xp_ledger_floor';
    END IF;
    RETURN NEW;
END
$$;
--;;
CREATE TRIGGER xp_ledger_floor BEFORE INSERT ON civ.xp_ledger
    FOR EACH ROW EXECUTE FUNCTION civ.xp_ledger_floor();
--;;

-- p_amount is the MAGNITUDE of the penalty (1..100). Returns: penalized | floor (nothing left to take: a zero marker is written) | duplicate.
CREATE FUNCTION civ.apply_xp_penalty(p_category text, p_source_ref text, p_amount integer DEFAULT 10) RETURNS text
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_user uuid := civ.current_tenant_id();
    v_have integer;
    v_take integer;
BEGIN
    IF p_category NOT IN ('focus', 'intellect', 'body', 'connection') OR p_amount IS NULL OR p_amount NOT BETWEEN 1 AND 100
       OR p_source_ref IS NULL OR p_source_ref !~ '^(decay|penalty):' OR char_length(p_source_ref) > 200 OR p_source_ref ~ '[\x01-\x1f\x7f]' THEN
        RAISE EXCEPTION 'invalid XP penalty' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('civ.xp_ledger'), hashtext(v_user::text));
    IF EXISTS (SELECT 1 FROM civ.xp_ledger WHERE user_id = v_user AND source_ref = p_source_ref) THEN
        RETURN 'duplicate';
    END IF;
    SELECT COALESCE(sum(amount), 0)::integer INTO v_have FROM civ.xp_ledger WHERE user_id = v_user AND category = p_category;
    v_take := LEAST(p_amount, GREATEST(v_have, 0));
    INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (v_user, -v_take, p_category, p_source_ref);
    IF v_take = 0 THEN
        RETURN 'floor';
    END IF;
    RETURN 'penalized';
END
$$;
--;;
REVOKE ALL ON FUNCTION civ.apply_xp_penalty(text, text, integer) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.apply_xp_penalty(text, text, integer) TO ai_civ_app;
--;;

-- Consecutive tenant-local days with at least one earning. Today without an award yet does not break the streak (the day is not over).
CREATE FUNCTION civ.xp_streak_days(p_user uuid) RETURNS integer
    LANGUAGE sql STABLE
    AS $$
    WITH tz AS (SELECT u.timezone AS z FROM civ.users u WHERE u.id = p_user),
    days AS (
        SELECT DISTINCT (l.created_at AT TIME ZONE (SELECT z FROM tz))::date AS d
        FROM civ.xp_ledger l WHERE l.user_id = p_user AND l.amount > 0
    ),
    today AS (SELECT (now() AT TIME ZONE (SELECT z FROM tz))::date AS d),
    anchor AS (
        SELECT CASE WHEN EXISTS (SELECT 1 FROM days, today WHERE days.d = today.d) THEN (SELECT d FROM today)
                    ELSE (SELECT d FROM today) - 1 END AS d
    ),
    run AS (
        SELECT days.d, (SELECT d FROM anchor) - days.d AS back FROM days WHERE days.d <= (SELECT d FROM anchor)
    )
    SELECT COALESCE(max(r.back) + 1, 0)::integer
    FROM run r
    WHERE r.back = (SELECT count(*) FROM run r2 WHERE r2.back < r.back)
$$;
--;;
REVOKE ALL ON FUNCTION civ.xp_streak_days(uuid) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.xp_streak_days(uuid) TO ai_civ_app;
