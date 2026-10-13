-- 0013: tasks and their decomposition (v2.0, stage 3, PR 3.1).
--   * civ.tasks       - a ROOT task (the goal the user named) and its STEPS (rows whose parent_task_id points at the root; one level only).
--                       status is plain text + CHECK like event_drafts (open | done | dropped); due is the step's own planned moment (wall-clock
--                       -> instant, computed by CODE, never by the model).
--   * civ.task_events - which calendar event is the slot of which task. Written ONLY by the confirmation of an event draft (the button).
--   * event_drafts.task_id - a draft made for a step: when the user confirms it, the event is linked to that step.
CREATE TABLE civ.tasks (
    id             uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id        uuid        NOT NULL,
    parent_task_id uuid,                                       -- NULL = a root task; otherwise a step of that root
    position       smallint    NOT NULL DEFAULT 0,             -- the order of the steps inside their root
    title          text        NOT NULL,
    status         text        NOT NULL DEFAULT 'open',
    due            timestamptz,
    persona_id     uuid,                                       -- the topic the task was made in (NULL = the main chat)
    source         text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_tasks PRIMARY KEY (id),
    CONSTRAINT uq_tasks_user_id_id UNIQUE (user_id, id),       -- target of composite tenant-safe FKs
    CONSTRAINT fk_tasks_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_tasks_parent FOREIGN KEY (user_id, parent_task_id)
        REFERENCES civ.tasks (user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_tasks_persona FOREIGN KEY (user_id, persona_id)
        REFERENCES civ.personas (user_id, id) ON DELETE SET NULL (persona_id),
    CONSTRAINT ck_tasks_title CHECK (civ.is_clean_title(title)),
    CONSTRAINT ck_tasks_status CHECK (status IN ('open', 'done', 'dropped')),
    CONSTRAINT ck_tasks_source CHECK (source IN ('chat_decompose', 'mini_app')),
    CONSTRAINT ck_tasks_not_own_parent CHECK (parent_task_id IS NULL OR parent_task_id <> id),
    CONSTRAINT ck_tasks_position CHECK (position >= 0)
);
--;;
CREATE INDEX ix_tasks_user_parent ON civ.tasks (user_id, parent_task_id, position);
--;;
CREATE INDEX ix_tasks_user_status_due ON civ.tasks (user_id, status, due);
--;;
-- the same goal is not decomposed twice while the first one is still open
CREATE UNIQUE INDEX uq_tasks_open_root_title ON civ.tasks (user_id, title) WHERE parent_task_id IS NULL AND status = 'open';
--;;
-- One level only: a step cannot have steps, and a task that has steps cannot become a step. (Runs as the invoker, so RLS applies; the composite
-- FK already guarantees the parent belongs to the same tenant.)
CREATE FUNCTION civ.tasks_guard_depth() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.parent_task_id IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM civ.tasks p WHERE p.user_id = NEW.user_id AND p.id = NEW.parent_task_id AND p.parent_task_id IS NOT NULL) THEN
            RAISE EXCEPTION 'tasks: a step cannot have steps' USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM civ.tasks c WHERE c.user_id = NEW.user_id AND c.parent_task_id = NEW.id) THEN
            RAISE EXCEPTION 'tasks: a task that has steps cannot become a step' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
--;;
CREATE TRIGGER trg_tasks_guard_depth
    BEFORE INSERT OR UPDATE OF parent_task_id ON civ.tasks
    FOR EACH ROW EXECUTE FUNCTION civ.tasks_guard_depth();
--;;
ALTER TABLE civ.tasks ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.tasks AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.tasks TO ai_civ_app;
--;;

CREATE TABLE civ.task_events (
    id         uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id    uuid        NOT NULL,
    task_id    uuid        NOT NULL,
    event_id   uuid        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_task_events PRIMARY KEY (id),
    CONSTRAINT fk_task_events_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_task_events_task FOREIGN KEY (user_id, task_id) REFERENCES civ.tasks (user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_task_events_event FOREIGN KEY (user_id, event_id) REFERENCES civ.events (user_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_task_events_link UNIQUE (user_id, task_id, event_id)
);
--;;
CREATE INDEX ix_task_events_user_event ON civ.task_events (user_id, event_id);
--;;
ALTER TABLE civ.task_events ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.task_events AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- a link is made or removed, never edited
GRANT SELECT, INSERT, DELETE ON civ.task_events TO ai_civ_app;
--;;

ALTER TABLE civ.event_drafts ADD COLUMN task_id uuid;
--;;
ALTER TABLE civ.event_drafts ADD CONSTRAINT fk_event_drafts_task FOREIGN KEY (user_id, task_id)
    REFERENCES civ.tasks (user_id, id) ON DELETE SET NULL (task_id);
