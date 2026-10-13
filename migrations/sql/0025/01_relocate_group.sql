-- 0025: moving a person to a NEW group (the old one is broken: the bot was removed, the person never got admin rights, the group was deleted...).
-- Until now a person owned exactly one group for good: register_tenant answered has_other_group / other_group_dormant.
--
-- civ.relocate_group(person, new chat) points the person's tenant at the new group. Like register_tenant it TRUSTS its caller about who the person is and
-- that the new group is theirs (the bot checks both with Telegram first: the group was made for them by the userbot and the old one is really broken).
-- What moves with the tenant: nothing is deleted. What is reset because it only had a meaning in the OLD group:
--   * personas.telegram_thread_id (forum topics): the personas stay (prompts, history), their topics are made again in the new group;
--   * messages.telegram_message_id and the pending time capsules' source_message_id: Telegram numbers messages per chat, so the new group
--     starts again from 1 and would collide with the old ids in the "this update was already stored" uniqueness (a new message would be dropped as a replay).
-- Outcomes: relocated | already_here (this is already the person's group) | no_tenant | chat_taken (the group belongs to somebody else).
-- Same two advisory locks in the same order as register_tenant (person, then group): overlapping calls serialise, no deadlock.
CREATE FUNCTION civ.relocate_group(p_telegram_user_id bigint, p_forum_chat_id bigint)
    RETURNS TABLE (tenant_id uuid, outcome text, old_chat_id bigint)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_id   uuid;
    v_chat bigint;
BEGIN
    IF p_telegram_user_id IS NULL OR p_telegram_user_id <= 0 THEN
        RAISE EXCEPTION 'relocate_group: telegram_user_id must be a positive Telegram user id'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_forum_chat_id IS NULL OR p_forum_chat_id >= -1000000000000 THEN
        RAISE EXCEPTION 'relocate_group: forum_chat_id must be a supergroup id (-100...)'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('civ.register_tenant.person:' || p_telegram_user_id::text, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended('civ.register_tenant.group:' || p_forum_chat_id::text, 0));

    SELECT u.id, u.forum_chat_id INTO v_id, v_chat FROM civ.users u WHERE u.telegram_user_id = p_telegram_user_id;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::uuid, 'no_tenant'::text, NULL::bigint;
        RETURN;
    END IF;
    IF v_chat = p_forum_chat_id THEN
        RETURN QUERY SELECT v_id, 'already_here'::text, v_chat;
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM civ.users x WHERE x.forum_chat_id = p_forum_chat_id) THEN
        RETURN QUERY SELECT v_id, 'chat_taken'::text, v_chat;
        RETURN;
    END IF;

    UPDATE civ.users SET forum_chat_id = p_forum_chat_id, group_state = 'active', group_state_changed_at = now() WHERE id = v_id;
    UPDATE civ.personas SET telegram_thread_id = NULL WHERE user_id = v_id AND telegram_thread_id IS NOT NULL;
    UPDATE civ.messages SET telegram_message_id = NULL WHERE user_id = v_id AND telegram_message_id IS NOT NULL;
    UPDATE civ.time_capsules SET source_message_id = NULL WHERE user_id = v_id AND status = 'pending' AND source_message_id IS NOT NULL;
    RETURN QUERY SELECT v_id, 'relocated'::text, v_chat;
END
$$;
--;;
REVOKE ALL ON FUNCTION civ.relocate_group(bigint, bigint) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.relocate_group(bigint, bigint) TO ai_civ_app;
