-- 0023 / downgrade. Penalty rows cannot exist under the old CHECK, so they are removed first (the earnings stay).
DROP FUNCTION IF EXISTS civ.xp_streak_days(uuid);
--;;
DROP FUNCTION IF EXISTS civ.apply_xp_penalty(text, text, integer);
--;;
DROP TRIGGER IF EXISTS xp_ledger_floor ON civ.xp_ledger;
--;;
DROP FUNCTION IF EXISTS civ.xp_ledger_floor();
--;;
DELETE FROM civ.xp_ledger WHERE amount <= 0;
--;;
CREATE OR REPLACE FUNCTION civ.xp_ledger_daily_cap() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('civ.xp_ledger'), hashtext(NEW.user_id::text));
    IF civ.xp_on_local_day(NEW.user_id, NEW.created_at) + NEW.amount > 100 THEN
        RAISE EXCEPTION 'daily XP cap (100) reached' USING ERRCODE = '23514', CONSTRAINT = 'ck_xp_ledger_daily_cap';
    END IF;
    RETURN NEW;
END
$$;
--;;
CREATE OR REPLACE FUNCTION civ.xp_on_local_day(p_user uuid, p_at timestamptz) RETURNS integer
    LANGUAGE sql STABLE
    AS $$
    SELECT COALESCE(sum(l.amount), 0)::integer
    FROM civ.xp_ledger l
    WHERE l.user_id = p_user
      AND (l.created_at AT TIME ZONE (SELECT u.timezone FROM civ.users u WHERE u.id = p_user))::date
        = (p_at AT TIME ZONE (SELECT u.timezone FROM civ.users u WHERE u.id = p_user))::date
$$;
--;;
ALTER TABLE civ.xp_ledger DROP CONSTRAINT ck_xp_ledger_amount;
--;;
ALTER TABLE civ.xp_ledger ADD CONSTRAINT ck_xp_ledger_amount CHECK (amount BETWEEN 1 AND 100);
