-- 0001 / 05 - social graph + ACL (foundation for the "Diplomat" pattern) and the hard schedule.
-- NOTE: the Diplomat functions themselves (cross-tenant, SECURITY DEFINER, audited) are a later migration.
-- They MUST check: contact.status = 'accepted', the grant is not revoked/expired, and write diplomat_audit_log.

CREATE TABLE civ.contacts (
    id              uuid               NOT NULL DEFAULT gen_random_uuid(),
    user_id         uuid               NOT NULL,      -- owner of the address book entry
    contact_user_id uuid               NOT NULL,
    alias           text,                              -- "Sasha"
    status          civ.contact_status NOT NULL DEFAULT 'pending',
    created_at      timestamptz        NOT NULL DEFAULT now(),
    CONSTRAINT pk_contacts PRIMARY KEY (id),
    CONSTRAINT fk_contacts_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_contacts_contact_user FOREIGN KEY (contact_user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_contacts_user_contact UNIQUE (user_id, contact_user_id),
    CONSTRAINT ck_contacts_not_self CHECK (user_id <> contact_user_id)
);
--;;

-- ACL: default deny. A row = "user_id allows grantee_user_id to request <scope>".
-- Example: Sasha -> emotional_state_label; Olya -> calendar_free_busy only.
CREATE TABLE civ.access_grants (
    id              uuid          NOT NULL DEFAULT gen_random_uuid(),
    user_id         uuid          NOT NULL,           -- who grants
    grantee_user_id uuid          NOT NULL,           -- to whom
    scope           civ.acl_scope NOT NULL,
    constraints     jsonb         NOT NULL DEFAULT '{}',   -- e.g. {"horizon_days": 7}
    granted_at      timestamptz   NOT NULL DEFAULT now(),
    expires_at      timestamptz,
    revoked_at      timestamptz,
    CONSTRAINT pk_access_grants PRIMARY KEY (id),
    CONSTRAINT fk_access_grants_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    -- grants are only possible to someone who is in the owner's contacts
    CONSTRAINT fk_access_grants_contact FOREIGN KEY (user_id, grantee_user_id)
        REFERENCES civ.contacts (user_id, contact_user_id) ON DELETE CASCADE,
    CONSTRAINT ck_access_grants_expiry CHECK (expires_at IS NULL OR expires_at > granted_at),
    CONSTRAINT ck_access_grants_revoked CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);
--;;
-- One active grant per (owner, grantee, scope). An expired-but-not-revoked grant must be revoked before re-granting.
CREATE UNIQUE INDEX uq_access_grants_active ON civ.access_grants (user_id, grantee_user_id, scope)
    WHERE revoked_at IS NULL;
--;;

-- Who asked what about whom. user_id = the owner of the data (sees this log). The application role gets SELECT only:
-- rows are written by the (future) Diplomat SECURITY DEFINER functions.
CREATE TABLE civ.diplomat_audit_log (
    id                bigint        GENERATED ALWAYS AS IDENTITY,
    user_id           uuid          NOT NULL,
    requester_user_id uuid,                            -- NULL after the requester deleted their account
    scope             civ.acl_scope NOT NULL,
    decision          text          NOT NULL,
    created_at        timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_diplomat_audit_log PRIMARY KEY (id),
    CONSTRAINT fk_diplomat_audit_log_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_diplomat_audit_log_requester FOREIGN KEY (requester_user_id) REFERENCES civ.users (id) ON DELETE SET NULL,
    CONSTRAINT ck_diplomat_audit_log_decision CHECK (decision IN ('allowed', 'denied'))
);
--;;
CREATE INDEX ix_diplomat_audit_log_user_created ON civ.diplomat_audit_log (user_id, created_at);
--;;

-- ============ Hard schedule (managed from the Telegram Mini App, never invented by the LLM) ============
CREATE TABLE civ.events (
    id              uuid                 NOT NULL DEFAULT gen_random_uuid(),
    user_id         uuid                 NOT NULL,
    persona_id      uuid,                              -- a workout belongs to the "Self-development" topic
    title           text                 NOT NULL,
    description     text,
    starts_at       timestamptz          NOT NULL,
    ends_at         timestamptz          NOT NULL,
    all_day         boolean              NOT NULL DEFAULT false,
    timezone        text                 NOT NULL,     -- IANA zone the event was planned in (RRULE/DST expansion)
    status          civ.event_status     NOT NULL DEFAULT 'planned',
    recurrence_rule text,                              -- RFC 5545 RRULE, expanded by code (series master only)
    parent_event_id uuid,                              -- exception row -> its series master
    original_start  timestamptz,                       -- which occurrence of the series this row replaces
    visibility      civ.event_visibility NOT NULL DEFAULT 'busy_only',
    created_via     text                 NOT NULL,
    created_at      timestamptz          NOT NULL DEFAULT now(),
    updated_at      timestamptz          NOT NULL DEFAULT now(),
    CONSTRAINT pk_events PRIMARY KEY (id),
    CONSTRAINT fk_events_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_events_persona FOREIGN KEY (user_id, persona_id)
        REFERENCES civ.personas (user_id, id) ON DELETE SET NULL (persona_id),
    CONSTRAINT fk_events_parent FOREIGN KEY (user_id, parent_event_id)
        REFERENCES civ.events (user_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_events_user_id_id UNIQUE (user_id, id),
    CONSTRAINT ck_events_period CHECK (ends_at > starts_at),
    CONSTRAINT ck_events_exception CHECK ((parent_event_id IS NULL) = (original_start IS NULL)),
    CONSTRAINT ck_events_exception_no_rule CHECK (parent_event_id IS NULL OR recurrence_rule IS NULL),
    CONSTRAINT ck_events_created_via CHECK (created_via IN ('mini_app', 'bot_confirmed'))
);
--;;
CREATE INDEX ix_events_user_starts ON civ.events (user_id, starts_at);
--;;
CREATE INDEX ix_events_user_persona_starts ON civ.events (user_id, persona_id, starts_at);
--;;
-- at most one exception row per occurrence of a series
CREATE UNIQUE INDEX uq_events_exception ON civ.events (user_id, parent_event_id, original_start)
    WHERE parent_event_id IS NOT NULL;
--;;
CREATE TRIGGER trg_events_touch_updated_at
    BEFORE UPDATE ON civ.events
    FOR EACH ROW EXECUTE FUNCTION civ.touch_updated_at();
--;;
CREATE TRIGGER trg_events_validate_timezone
    BEFORE INSERT OR UPDATE OF timezone ON civ.events
    FOR EACH ROW EXECUTE FUNCTION civ.validate_timezone();
