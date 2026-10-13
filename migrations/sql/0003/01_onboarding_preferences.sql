-- 0003 / 01 - onboarding interview state + per-user proactive preferences.
--
-- users.onboarding_*: the interview is a fixed script, so its state is just (state, step, draft answers). The state lives in
-- PostgreSQL (not in process memory): a restart of the bot never loses a half-finished interview.
-- Rows that exist BEFORE this migration are grandfathered as 'done' (they were let into the ecosystem already); every
-- row inserted afterwards starts as 'pending' (the column default is switched below). An operator can re-run the
-- interview for someone with: UPDATE civ.users SET onboarding_state = 'pending', onboarding_step = 0, onboarding_draft = '{}'.
--
-- users.quiet_hours_*: the local-time window in which the bot never writes first (it may wrap around midnight).
-- proactive_schedule: at what local time each proactive trigger fires for this user (one nudge per trigger per day).

ALTER TABLE civ.users
    ADD COLUMN onboarding_state        text        NOT NULL DEFAULT 'done',
    ADD COLUMN onboarding_step         smallint    NOT NULL DEFAULT 0,
    ADD COLUMN onboarding_draft        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN onboarding_completed_at timestamptz,
    ADD COLUMN quiet_hours_start       time        NOT NULL DEFAULT '22:00',
    ADD COLUMN quiet_hours_end         time        NOT NULL DEFAULT '08:00';
--;;
ALTER TABLE civ.users ALTER COLUMN onboarding_state SET DEFAULT 'pending';
--;;
ALTER TABLE civ.users
    ADD CONSTRAINT ck_users_onboarding_state CHECK (onboarding_state IN ('pending', 'in_progress', 'done')),
    ADD CONSTRAINT ck_users_onboarding_step CHECK (onboarding_step BETWEEN 0 AND 50),
    ADD CONSTRAINT ck_users_quiet_hours CHECK (quiet_hours_start <> quiet_hours_end);
--;;

CREATE TABLE civ.proactive_schedule (
    id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id       uuid        NOT NULL,
    trigger_key   text        NOT NULL,                  -- key of ai_civ.bot.proactive.TRIGGERS
    local_time    time        NOT NULL,                  -- in the tenant's own time zone (users.timezone)
    enabled       boolean     NOT NULL DEFAULT true,
    last_fired_on date,                                  -- tenant-local date of the last dispatch: at most one per day
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_proactive_schedule PRIMARY KEY (id),
    CONSTRAINT fk_proactive_schedule_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_proactive_schedule_user_trigger UNIQUE (user_id, trigger_key),
    CONSTRAINT ck_proactive_schedule_key CHECK (trigger_key ~ '^[a-z][a-z0-9_]{1,39}$')
);
--;;
ALTER TABLE civ.proactive_schedule ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.proactive_schedule AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.proactive_schedule TO ai_civ_app;
