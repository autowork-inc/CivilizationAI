-- 0026: support for the mobile app (a person WITHOUT Telegram), additive and safe for the running bot.
--   * users.telegram_user_id becomes NULLable; a person is identified by Telegram OR an e-mail OR an external sign-in (CHECK).
--     Every Telegram code path resolves a tenant BY a Telegram id, so a NULL never matches anything there; the places that iterate over
--     ALL tenants (Beat dispatchers) are guarded in code (they need forum_chat_id, which an app person does not have).
--   * messages.client_message_id: the id the app generates for a message it sends. The partial unique index makes a retried request
--     (a flaky network) a no-op instead of a second message. messages.reply_to_id: which user message an assistant message answers
--     (a duplicate request is answered from the stored reply, without the model). messages.cards: the cards (buttons) that went with an assistant
--     reply, as the mobile client got them (JSON), so history and a repeated request show the same thing.
--   * user_sessions: refresh tokens of the app (only a hash is stored). Rows are tenant rows (RLS); the token handed to the app carries the user id,
--     so a refresh opens the tenant session first and then looks the session up.
--   * civ.register_app_user / civ.resolve_tenant_by_email: SECURITY DEFINER, because the application role cannot INSERT into users (RLS)
--     and cannot look a tenant up before it knows the tenant.
-- The file is pure ASCII (non-ASCII titles are written as U& escapes).

ALTER TABLE civ.users ALTER COLUMN telegram_user_id DROP NOT NULL;
--;;
ALTER TABLE civ.users
    ADD COLUMN email                  text,
    ADD COLUMN pass_hash              text,
    ADD COLUMN external_auth_provider text,
    ADD COLUMN external_auth_id       text;
--;;
ALTER TABLE civ.users
    ADD CONSTRAINT ck_users_identity CHECK (telegram_user_id IS NOT NULL OR email IS NOT NULL OR external_auth_id IS NOT NULL),
    ADD CONSTRAINT ck_users_email CHECK (email IS NULL OR (email = btrim(email) AND char_length(email) BETWEEN 3 AND 254 AND email ~ '^[^@[:space:]]+@[^@[:space:]]+$')),
    ADD CONSTRAINT ck_users_external CHECK ((external_auth_provider IS NULL) = (external_auth_id IS NULL)),
    ADD CONSTRAINT ck_users_pass_hash CHECK (pass_hash IS NULL OR (email IS NOT NULL AND btrim(pass_hash) <> ''));
--;;
CREATE UNIQUE INDEX uq_users_email_lower ON civ.users (lower(email)) WHERE email IS NOT NULL;
--;;
CREATE UNIQUE INDEX uq_users_external_auth ON civ.users (external_auth_provider, external_auth_id) WHERE external_auth_id IS NOT NULL;
--;;

ALTER TABLE civ.messages
    ADD COLUMN client_message_id uuid,
    ADD COLUMN reply_to_id       uuid,
    ADD COLUMN cards             jsonb;
--;;
ALTER TABLE civ.messages
    ADD CONSTRAINT fk_messages_reply_to FOREIGN KEY (user_id, reply_to_id) REFERENCES civ.messages (user_id, id) ON DELETE SET NULL (reply_to_id),
    ADD CONSTRAINT ck_messages_reply_not_self CHECK (reply_to_id IS NULL OR reply_to_id <> id);
--;;
CREATE UNIQUE INDEX uq_messages_user_client_id ON civ.messages (user_id, client_message_id) WHERE client_message_id IS NOT NULL;
--;;
-- ONE answer per user message: two requests that both got as far as generating (two processes, a crash in between) cannot both store a reply.
CREATE UNIQUE INDEX uq_messages_user_reply_to ON civ.messages (user_id, reply_to_id) WHERE reply_to_id IS NOT NULL;
--;;

CREATE TABLE civ.user_sessions (
    id                 uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id            uuid        NOT NULL,
    refresh_token_hash text        NOT NULL,
    user_agent         text,
    expires_at         timestamptz NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_user_sessions PRIMARY KEY (id),
    CONSTRAINT fk_user_sessions_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_user_sessions_hash CHECK (btrim(refresh_token_hash) <> ''),
    CONSTRAINT ck_user_sessions_agent CHECK (user_agent IS NULL OR char_length(user_agent) <= 300)
);
--;;
CREATE INDEX ix_user_sessions_user_expires ON civ.user_sessions (user_id, expires_at);
--;;
ALTER TABLE civ.user_sessions ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.user_sessions AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.user_sessions TO ai_civ_app;
--;;

-- register_app_user: creates the tenant of an app person and everything the ecosystem expects of a tenant who is "in":
-- onboarding is done (the app has no interview: the person is let in at once), the two system personas (settings, feedback) without a
-- topic, and the character profile. Outcome 'created' | 'email_taken' (a value, not an exception: a race of two sign-ups for one address
-- is an ordinary event). The caller passes an ALREADY HASHED password.
CREATE FUNCTION civ.register_app_user(p_email text, p_pass_hash text, p_timezone text DEFAULT 'UTC')
    RETURNS TABLE (tenant_id uuid, outcome text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_email text := lower(btrim(coalesce(p_email, '')));
    v_id    uuid;
    v_set   text := U&'\2699\FE0F \041D\0430\0441\0442\0440\043E\0439\043A\0438';
    v_fb    text := U&'\+01F4A1 \0418\0434\0435\0438 \0438 \043E\0442\0437\044B\0432\044B';
BEGIN
    IF v_email = '' OR p_pass_hash IS NULL OR btrim(p_pass_hash) = '' THEN
        RAISE EXCEPTION 'register_app_user: e-mail and password hash are required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('civ.register_app_user:' || v_email, 0));
    IF EXISTS (SELECT 1 FROM civ.users u WHERE lower(u.email) = v_email) THEN
        RETURN QUERY SELECT NULL::uuid, 'email_taken'::text;
        RETURN;
    END IF;

    INSERT INTO civ.users (email, pass_hash, timezone, onboarding_state, onboarding_completed_at)
    VALUES (v_email, p_pass_hash, coalesce(nullif(btrim(p_timezone), ''), 'UTC'), 'done', now())
    RETURNING id INTO v_id;

    INSERT INTO civ.personas (user_id, code, title, system_prompt) VALUES
        (v_id, 'settings', v_set,
         format(E'You are the user''s personal AI avatar in their topic "%s".\nTopic focus: %s\nStay inside this focus and keep it concrete.',
                v_set, 'settings: what the avatar remembers about the user, how it behaves, how to change it')),
        (v_id, 'feedback', v_fb,
         format(E'You are the user''s personal AI avatar in their topic "%s".\nTopic focus: %s\nStay inside this focus and keep it concrete.',
                v_fb, 'feedback: ideas, wishes and complaints about the product; the messages go to the author of the project'));

    INSERT INTO civ.character_profiles (user_id) VALUES (v_id);
    RETURN QUERY SELECT v_id, 'created'::text;
END
$$;
--;;
REVOKE ALL ON FUNCTION civ.register_app_user(text, text, text) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.register_app_user(text, text, text) TO ai_civ_app;
--;;

-- resolve_tenant_by_email: what the sign-in needs BEFORE it knows the tenant: id, the stored hash (compared in code, never returned to a client) and the
-- time zone. Only rows that have a password.
CREATE FUNCTION civ.resolve_tenant_by_email(p_email text)
    RETURNS TABLE (id uuid, pass_hash text, timezone text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
    SELECT u.id, u.pass_hash, u.timezone
    FROM civ.users u
    WHERE lower(u.email) = lower(btrim(coalesce(p_email, ''))) AND u.pass_hash IS NOT NULL
$$;
--;;
REVOKE ALL ON FUNCTION civ.resolve_tenant_by_email(text) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.resolve_tenant_by_email(text) TO ai_civ_app;
--;;

-- publish_app_notification: the one write path of the background tasks for a person who is reached by push (FCM / APNs) instead of Telegram. The application role
-- cannot INSERT into notification_outbox (rows are made by functions only); the tenant is the caller's (current_tenant_id), never an argument.
-- Outcome: 'queued' | 'duplicate' (the same idempotency key was queued before: a retried task adds nothing).
CREATE FUNCTION civ.publish_app_notification(p_kind text, p_payload jsonb, p_idempotency_key text)
    RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me  uuid := civ.current_tenant_id();
    v_id  bigint;
BEGIN
    IF p_kind IS NULL OR p_kind !~ '^[a-z][a-z0-9_]{1,39}$' THEN
        RAISE EXCEPTION 'publish_app_notification: bad kind' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'publish_app_notification: payload must be a JSON object' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_idempotency_key IS NULL OR btrim(p_idempotency_key) = '' OR char_length(p_idempotency_key) > 200 THEN
        RAISE EXCEPTION 'publish_app_notification: bad idempotency key' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    INSERT INTO civ.notification_outbox (user_id, kind, payload, idempotency_key)
    VALUES (v_me, p_kind, p_payload, p_idempotency_key)
    ON CONFLICT (user_id, idempotency_key) DO NOTHING
    RETURNING id INTO v_id;
    RETURN CASE WHEN v_id IS NULL THEN 'duplicate' ELSE 'queued' END;
END
$$;
--;;
REVOKE ALL ON FUNCTION civ.publish_app_notification(text, jsonb, text) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.publish_app_notification(text, jsonb, text) TO ai_civ_app;
