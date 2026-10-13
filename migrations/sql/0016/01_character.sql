-- 0016: character profile and the XP ledger (v2.0, stage 2, PR 2.1).
--   * civ.character_profiles - one row per tenant: the display name of the character (NULL = fall back to the Telegram first name in the API),
--                              an optional avatar (<= 256 KiB, the API re-encodes it to WebP) and the four visibility flags for the future Tavern.
--   * civ.xp_ledger          - append-only list of experience awards. UNIQUE (user_id, source_ref) makes every award idempotent
--                              ("task:<id>", "card:<id>:<date>"); a trigger caps ONE tenant-local day at 100 XP.
--   * civ.award_xp           - the one write path of the services: returns a code (awarded | duplicate | daily_cap), never raises for those two.
--   * civ.character_progress - security_invoker view: total XP, level (100 XP = one level), progress inside the level, XP per branch, XP today.

CREATE TABLE civ.character_profiles (
    user_id         uuid        NOT NULL,
    character_name  text,
    avatar_bytes    bytea,
    show_level      boolean     NOT NULL DEFAULT true,
    show_mood       boolean     NOT NULL DEFAULT false,
    show_interests  boolean     NOT NULL DEFAULT false,
    open_for_intros boolean     NOT NULL DEFAULT false,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_character_profiles PRIMARY KEY (user_id),
    CONSTRAINT fk_character_profiles_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_character_profiles_name CHECK (character_name IS NULL OR civ.is_clean_name(character_name)),
    CONSTRAINT ck_character_profiles_avatar CHECK (avatar_bytes IS NULL OR octet_length(avatar_bytes) BETWEEN 1 AND 262144)
);
--;;
ALTER TABLE civ.character_profiles ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.character_profiles AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.character_profiles TO ai_civ_app;
--;;

CREATE TABLE civ.xp_ledger (
    id         uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id    uuid        NOT NULL,
    amount     integer     NOT NULL DEFAULT 10,
    category   text        NOT NULL,
    source_ref text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_xp_ledger PRIMARY KEY (id),
    CONSTRAINT uq_xp_ledger_user_id_id UNIQUE (user_id, id),
    CONSTRAINT uq_xp_ledger_source UNIQUE (user_id, source_ref),       -- an event is paid once
    CONSTRAINT fk_xp_ledger_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_xp_ledger_amount CHECK (amount BETWEEN 1 AND 100),
    CONSTRAINT ck_xp_ledger_category CHECK (category IN ('focus', 'intellect', 'body', 'connection')),
    CONSTRAINT ck_xp_ledger_source CHECK (btrim(source_ref) <> '' AND char_length(source_ref) <= 200 AND source_ref !~ '[\x01-\x1f\x7f]')
);
--;;
CREATE INDEX ix_xp_ledger_user_created ON civ.xp_ledger (user_id, created_at);
--;;
ALTER TABLE civ.xp_ledger ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.xp_ledger AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- append-only: an award can be neither edited nor taken back by the application (that would reopen the daily cap)
GRANT SELECT, INSERT ON civ.xp_ledger TO ai_civ_app;
--;;

-- XP already awarded on the tenant-local calendar day of p_at. The day is the tenant's own (users.timezone), like every other "today" here.
CREATE FUNCTION civ.xp_on_local_day(p_user uuid, p_at timestamptz) RETURNS integer
    LANGUAGE sql STABLE
    AS $$
    SELECT COALESCE(sum(l.amount), 0)::integer
    FROM civ.xp_ledger l
    WHERE l.user_id = p_user
      AND (l.created_at AT TIME ZONE (SELECT u.timezone FROM civ.users u WHERE u.id = p_user))::date
        = (p_at AT TIME ZONE (SELECT u.timezone FROM civ.users u WHERE u.id = p_user))::date
$$;
--;;
-- The hard rule, whoever inserts: at most 100 XP per tenant-local day. The advisory lock serialises one tenant's inserts, so two parallel
-- awards cannot both read "90" and both pass.
CREATE FUNCTION civ.xp_ledger_daily_cap() RETURNS trigger
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
CREATE TRIGGER xp_ledger_daily_cap BEFORE INSERT ON civ.xp_ledger
    FOR EACH ROW EXECUTE FUNCTION civ.xp_ledger_daily_cap();
--;;

-- The write path of the services. SECURITY INVOKER on purpose: RLS stays in force and "whose XP" is only civ.current_tenant_id().
CREATE FUNCTION civ.award_xp(p_category text, p_source_ref text, p_amount integer DEFAULT 10) RETURNS text
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_user uuid := civ.current_tenant_id();
BEGIN
    IF p_category NOT IN ('focus', 'intellect', 'body', 'connection') OR p_amount IS NULL OR p_amount NOT BETWEEN 1 AND 100
       OR p_source_ref IS NULL OR btrim(p_source_ref) = '' OR char_length(p_source_ref) > 200 OR p_source_ref ~ '[\x01-\x1f\x7f]' THEN
        RAISE EXCEPTION 'invalid XP award' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('civ.xp_ledger'), hashtext(v_user::text));
    IF EXISTS (SELECT 1 FROM civ.xp_ledger WHERE user_id = v_user AND source_ref = p_source_ref) THEN
        RETURN 'duplicate';
    END IF;
    IF civ.xp_on_local_day(v_user, now()) + p_amount > 100 THEN
        RETURN 'daily_cap';
    END IF;
    INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (v_user, p_amount, p_category, p_source_ref);
    RETURN 'awarded';
END
$$;
--;;
REVOKE ALL ON FUNCTION civ.award_xp(text, text, integer) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.award_xp(text, text, integer) TO ai_civ_app;
--;;
REVOKE ALL ON FUNCTION civ.xp_on_local_day(uuid, timestamptz) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.xp_on_local_day(uuid, timestamptz) TO ai_civ_app;
--;;

CREATE VIEW civ.character_progress WITH (security_invoker = true) AS
SELECT u.id AS user_id,
       t.total_xp,
       t.total_xp / 100 + 1 AS level,                       -- 100 XP = one level, linear
       t.total_xp % 100     AS current_level_xp,            -- progress inside the level, 0..99 (also the percentage)
       jsonb_build_object('focus', t.focus, 'intellect', t.intellect, 'body', t.body, 'connection', t.connection) AS xp_by_category,
       civ.xp_on_local_day(u.id, now()) AS xp_today
FROM civ.users u
CROSS JOIN LATERAL (
    SELECT COALESCE(sum(l.amount), 0)::integer AS total_xp,
           COALESCE(sum(l.amount) FILTER (WHERE l.category = 'focus'), 0)::integer AS focus,
           COALESCE(sum(l.amount) FILTER (WHERE l.category = 'intellect'), 0)::integer AS intellect,
           COALESCE(sum(l.amount) FILTER (WHERE l.category = 'body'), 0)::integer AS body,
           COALESCE(sum(l.amount) FILTER (WHERE l.category = 'connection'), 0)::integer AS connection
    FROM civ.xp_ledger l WHERE l.user_id = u.id
) t;
--;;
GRANT SELECT ON civ.character_progress TO ai_civ_app;
