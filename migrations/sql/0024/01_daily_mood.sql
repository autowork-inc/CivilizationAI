-- 0024: the mood of each day, read from the user's own words (v2.0, "daily mood chart"), and users.checkin_per_day.
--   * civ.daily_mood - one row per (user, LOCAL calendar day): score -2 (heavy) .. +2 (bright), the dominant word of the closed vocabulary civ.shared_mood (or NULL),
--                      and how many of the user's messages of that day were read (message_count). It is an ESTIMATE made by a model from the day's dialogue and voice
--                      transcripts, so it is NEVER shared: no SECURITY DEFINER function reads it (the Diplomat and the Tavern use only what the user CONFIRMED,
--                      emotional_states.source = 'user_confirmed'); it only feeds the user's own dashboard. The job re-scores a day only when message_count changes.

CREATE TABLE civ.daily_mood (
    id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id       uuid        NOT NULL,
    local_date    date        NOT NULL,
    score         smallint    NOT NULL,
    mood          civ.shared_mood,
    message_count integer     NOT NULL,
    scored_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_daily_mood PRIMARY KEY (id),
    CONSTRAINT uq_daily_mood_user_id_id UNIQUE (user_id, id),
    CONSTRAINT uq_daily_mood_user_date UNIQUE (user_id, local_date),
    CONSTRAINT fk_daily_mood_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_daily_mood_score CHECK (score BETWEEN -2 AND 2),
    CONSTRAINT ck_daily_mood_count CHECK (message_count > 0)
);
--;;
ALTER TABLE civ.daily_mood ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.daily_mood AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT ON civ.daily_mood TO ai_civ_app;
--;;
GRANT UPDATE (score, mood, message_count, scored_at) ON civ.daily_mood TO ai_civ_app;
--;;
-- The user's own say over the check-ins (screen "Напоминания"): NULL = the system default (3-4 random slots a day inside the active hours), 0 = none, 1-4 = that many.
ALTER TABLE civ.users ADD COLUMN checkin_per_day smallint;
--;;
ALTER TABLE civ.users ADD CONSTRAINT ck_users_checkin_per_day CHECK (checkin_per_day IS NULL OR checkin_per_day BETWEEN 0 AND 4);
