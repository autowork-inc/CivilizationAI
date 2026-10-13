-- 0018: workout tracker (v2.0, stage 4, PR 4.1).
--   * civ.exercises        - the canonical dictionary of ONE tenant: a clean name, a muscle group (from a closed list: it picks the step of the
--                            progressive overload) and the synonyms ("leg press", "zhim nogami") the code resolves a name against.
--   * civ.workout_sessions - one logged workout; source_message_id (the user's message) makes a redelivered update unable to log it twice.
--   * civ.workout_sets     - one set: weight, reps, optional RPE and the optional target of reps (the "all target reps done" rule reads it).
-- The numbers come from the user's own words (the code verifies them before the insert); this schema only refuses nonsense and foreign rows.

-- Synonyms of an exercise: at most 20, each one a clean line (civ.is_clean_title: 1-80 characters, no control and no bidi characters).
-- NOT EXISTS (not bool_and): an empty array is valid and bool_and over no rows is NULL.
CREATE FUNCTION civ.exercise_aliases_valid(p_aliases text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT p_aliases IS NOT NULL
       AND COALESCE(array_ndims(p_aliases), 1) = 1
       AND cardinality(p_aliases) <= 20
       AND NOT EXISTS (SELECT 1 FROM unnest(p_aliases) AS a WHERE a IS NULL OR NOT civ.is_clean_title(a))
$$;
--;;
CREATE TABLE civ.exercises (
    id           uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL,
    name         text        NOT NULL,
    muscle_group text        NOT NULL DEFAULT 'other',
    aliases      text[]      NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_exercises PRIMARY KEY (id),
    CONSTRAINT uq_exercises_user_id_id UNIQUE (user_id, id),       -- target of composite tenant-safe FKs
    CONSTRAINT uq_exercises_user_name UNIQUE (user_id, name),
    CONSTRAINT fk_exercises_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_exercises_name CHECK (civ.is_clean_title(name)),
    CONSTRAINT ck_exercises_group CHECK (muscle_group IN ('chest', 'back', 'shoulders', 'arms', 'legs', 'core', 'other')),
    CONSTRAINT ck_exercises_aliases CHECK (civ.exercise_aliases_valid(aliases))
);
--;;
ALTER TABLE civ.exercises ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.exercises AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.exercises TO ai_civ_app;
--;;

CREATE TABLE civ.workout_sessions (
    id                uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id           uuid        NOT NULL,
    started_at        timestamptz NOT NULL DEFAULT now(),
    notes             text        NOT NULL DEFAULT '',
    source_message_id uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_workout_sessions PRIMARY KEY (id),
    CONSTRAINT uq_workout_sessions_user_id_id UNIQUE (user_id, id),
    CONSTRAINT fk_workout_sessions_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_workout_sessions_message FOREIGN KEY (user_id, source_message_id)
        REFERENCES civ.messages (user_id, id) ON DELETE SET NULL (source_message_id),
    CONSTRAINT ck_workout_sessions_notes CHECK (char_length(notes) <= 500 AND notes !~ E'[\x01-\x09\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]')
);
--;;
-- one message logs at most one workout: a replayed update finds the session instead of adding a second one
CREATE UNIQUE INDEX uq_workout_sessions_message ON civ.workout_sessions (user_id, source_message_id) WHERE source_message_id IS NOT NULL;
--;;
CREATE INDEX ix_workout_sessions_user_started ON civ.workout_sessions (user_id, started_at);
--;;
ALTER TABLE civ.workout_sessions ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.workout_sessions AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, DELETE ON civ.workout_sessions TO ai_civ_app;
--;;

CREATE TABLE civ.workout_sets (
    id          uuid          NOT NULL DEFAULT gen_random_uuid(),
    user_id     uuid          NOT NULL,
    session_id  uuid          NOT NULL,
    exercise_id uuid          NOT NULL,
    set_order   smallint      NOT NULL,                  -- 1.. within (session, exercise)
    weight      numeric(7,2)  NOT NULL DEFAULT 0,        -- kg; 0 = the body weight / no load
    reps        smallint      NOT NULL,
    rpe         numeric(3,1),
    target_reps smallint,
    created_at  timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_workout_sets PRIMARY KEY (id),
    CONSTRAINT uq_workout_sets_order UNIQUE (user_id, session_id, exercise_id, set_order),
    CONSTRAINT fk_workout_sets_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_workout_sets_session FOREIGN KEY (user_id, session_id) REFERENCES civ.workout_sessions (user_id, id) ON DELETE CASCADE,
    -- NO ACTION (not CASCADE): an exercise with a history cannot be deleted from under it
    CONSTRAINT fk_workout_sets_exercise FOREIGN KEY (user_id, exercise_id) REFERENCES civ.exercises (user_id, id),
    CONSTRAINT ck_workout_sets_order CHECK (set_order BETWEEN 1 AND 100),
    CONSTRAINT ck_workout_sets_weight CHECK (weight >= 0 AND weight <= 1000),
    CONSTRAINT ck_workout_sets_reps CHECK (reps BETWEEN 1 AND 500),
    CONSTRAINT ck_workout_sets_rpe CHECK (rpe IS NULL OR rpe BETWEEN 1 AND 10),
    CONSTRAINT ck_workout_sets_target CHECK (target_reps IS NULL OR target_reps BETWEEN 1 AND 500)
);
--;;
CREATE INDEX ix_workout_sets_user_exercise ON civ.workout_sets (user_id, exercise_id, session_id);
--;;
CREATE INDEX ix_workout_sets_user_session ON civ.workout_sets (user_id, session_id);
--;;
ALTER TABLE civ.workout_sets ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.workout_sets AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- a set is made or removed, never edited (a wrong set is deleted and made again)
GRANT SELECT, INSERT, DELETE ON civ.workout_sets TO ai_civ_app;
