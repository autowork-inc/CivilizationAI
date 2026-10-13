-- 0001 / 02 - tenants and personas (Telegram forum topics).

-- users.id IS the tenant id (the only table without a separate user_id column).
CREATE TABLE civ.users (
    id               uuid        NOT NULL DEFAULT gen_random_uuid(),
    telegram_user_id bigint      NOT NULL,
    forum_chat_id    bigint,
    timezone         text        NOT NULL DEFAULT 'UTC',
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uq_users_telegram_user_id UNIQUE (telegram_user_id),
    CONSTRAINT uq_users_forum_chat_id UNIQUE (forum_chat_id)
);
--;;
CREATE TRIGGER trg_users_validate_timezone
    BEFORE INSERT OR UPDATE OF timezone ON civ.users
    FOR EACH ROW EXECUTE FUNCTION civ.validate_timezone();
--;;

CREATE TABLE civ.personas (
    id                 uuid    NOT NULL DEFAULT gen_random_uuid(),
    user_id            uuid    NOT NULL,
    code               text    NOT NULL,           -- 'foodguide', 'job_hunt', 'discipline', 'settings'
    title              text    NOT NULL,
    system_prompt      text    NOT NULL,
    telegram_thread_id bigint,                      -- message_thread_id of the forum topic
    is_active          boolean NOT NULL DEFAULT true,
    CONSTRAINT pk_personas PRIMARY KEY (id),
    CONSTRAINT fk_personas_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_personas_user_code UNIQUE (user_id, code),
    CONSTRAINT uq_personas_user_thread UNIQUE (user_id, telegram_thread_id),
    CONSTRAINT uq_personas_user_id_id UNIQUE (user_id, id)   -- target of composite tenant-safe FKs
);
--;;

-- Tenant bootstrap. users is protected by RLS (id = current tenant), so the orchestrator cannot look a tenant
-- up by Telegram id before it knows the tenant. This SECURITY DEFINER function (runs as the table owner, which
-- bypasses RLS because FORCE ROW LEVEL SECURITY is intentionally NOT used) returns ONLY the uuid.
CREATE FUNCTION civ.resolve_tenant_id(p_telegram_user_id bigint DEFAULT NULL, p_forum_chat_id bigint DEFAULT NULL)
    RETURNS uuid
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_id uuid;
BEGIN
    IF num_nonnulls(p_telegram_user_id, p_forum_chat_id) <> 1 THEN
        RAISE EXCEPTION 'resolve_tenant_id: pass exactly one of telegram_user_id / forum_chat_id'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT u.id INTO v_id
    FROM civ.users u
    WHERE (p_telegram_user_id IS NOT NULL AND u.telegram_user_id = p_telegram_user_id)
       OR (p_forum_chat_id    IS NOT NULL AND u.forum_chat_id    = p_forum_chat_id);
    RETURN v_id;
END
$$;
