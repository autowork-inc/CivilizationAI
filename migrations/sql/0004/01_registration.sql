-- 0004 / 01 - automatic registration (Zero-SQL onboarding) + the state of the tenant's group.
--
-- civ.users is protected by RLS (id = current tenant), and a brand-new person has no tenant yet, so the bot cannot create
-- the row under the runtime role. civ.register_tenant (SECURITY DEFINER, like resolve_tenant_id / list_tenants) does it
-- atomically. It trusts its caller about WHO the Telegram user is: the bot has to verify that against Telegram first
-- (the person who created the group, a forum, the bot is an admin, nobody else is in it) - see ai_civ.bot.registration.
--
-- users.group_state is the bot's switch for the group:
--   active  - normal operation;
--   frozen  - somebody other than the owner and the bot is in the group: the bot answers nothing there and sends nothing
--             (the replies carry the owner's private memory) until the group is private again;
--   dormant - the bot was removed from the group: nothing is sent (it would only fail); adding it back to the SAME group
--             reactivates the tenant.

ALTER TABLE civ.users
    ADD COLUMN group_state            text        NOT NULL DEFAULT 'active',
    ADD COLUMN group_state_changed_at timestamptz NOT NULL DEFAULT now();
--;;
ALTER TABLE civ.users
    ADD CONSTRAINT ck_users_group_state CHECK (group_state IN ('active', 'frozen', 'dormant'));
--;;

-- Outcomes (text, the caller decides what to tell the user):
--   created              new tenant (onboarding_state = 'pending')
--   already_registered   this person already owns exactly this group (events are delivered more than once)
--   reactivated          same group, was dormant (the bot was removed and added back)
--   attached_chat        the person existed without a group (registered before this migration) and now has one
--   has_other_group      the person already owns another group: one group per person, this one is refused
--   other_group_dormant  ... and the bot was removed from that one (moving to a new group is not supported yet)
--   chat_taken           this group belongs to somebody else
-- Concurrency: two advisory locks, ALWAYS taken in the same order (person, then group), so overlapping registrations
-- serialise and can never deadlock; the UNIQUE constraints stay the last line of defence.
CREATE FUNCTION civ.register_tenant(p_telegram_user_id bigint, p_forum_chat_id bigint)
    RETURNS TABLE (tenant_id uuid, outcome text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_id    uuid;
    v_chat  bigint;
    v_state text;
BEGIN
    IF p_telegram_user_id IS NULL OR p_telegram_user_id <= 0 THEN
        RAISE EXCEPTION 'register_tenant: telegram_user_id must be a positive Telegram user id'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- a supergroup (every forum is one) has an id of the form -100<internal id>, i.e. below -1000000000000
    IF p_forum_chat_id IS NULL OR p_forum_chat_id >= -1000000000000 THEN
        RAISE EXCEPTION 'register_tenant: forum_chat_id must be a supergroup id (-100...)'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('civ.register_tenant.person:' || p_telegram_user_id::text, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended('civ.register_tenant.group:' || p_forum_chat_id::text, 0));

    SELECT u.id, u.forum_chat_id, u.group_state INTO v_id, v_chat, v_state
    FROM civ.users u
    WHERE u.telegram_user_id = p_telegram_user_id;

    IF FOUND THEN
        IF v_chat = p_forum_chat_id THEN
            IF v_state = 'dormant' THEN
                UPDATE civ.users SET group_state = 'active', group_state_changed_at = now() WHERE id = v_id;
                RETURN QUERY SELECT v_id, 'reactivated'::text;
            ELSE
                RETURN QUERY SELECT v_id, 'already_registered'::text;
            END IF;
        ELSIF v_chat IS NOT NULL THEN
            RETURN QUERY SELECT v_id, CASE WHEN v_state = 'dormant' THEN 'other_group_dormant' ELSE 'has_other_group' END;
        ELSIF EXISTS (SELECT 1 FROM civ.users x WHERE x.forum_chat_id = p_forum_chat_id) THEN
            RETURN QUERY SELECT v_id, 'chat_taken'::text;
        ELSE
            UPDATE civ.users SET forum_chat_id = p_forum_chat_id WHERE id = v_id;
            RETURN QUERY SELECT v_id, 'attached_chat'::text;
        END IF;
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM civ.users x WHERE x.forum_chat_id = p_forum_chat_id) THEN
        RETURN QUERY SELECT NULL::uuid, 'chat_taken'::text;
        RETURN;
    END IF;

    INSERT INTO civ.users (telegram_user_id, forum_chat_id) VALUES (p_telegram_user_id, p_forum_chat_id)
    RETURNING id INTO v_id;
    RETURN QUERY SELECT v_id, 'created'::text;
END
$$;
--;;
REVOKE ALL ON FUNCTION civ.register_tenant(bigint, bigint) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.register_tenant(bigint, bigint) TO ai_civ_app;
