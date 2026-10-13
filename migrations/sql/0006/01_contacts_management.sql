-- 0006 / 01 - managing the address book from the Mini App (Module 5, step 3): revoke one invite, rename / block / unblock / remove a
-- contact, switch a grant on and off.
--
-- Same rules as 0005 (the application role is NOT trusted to follow them):
--   * civ.contacts stays closed for INSERT / UPDATE: everything below is a SECURITY DEFINER function, and WHO is calling comes only from
--     civ.current_tenant_id() - never from an argument. A contact id is only a handle to the CALLER's own row (user_id = caller).
--   * every function that touches a pair takes civ.lock_pair(caller, other) FIRST (symmetric: A blocking B while B accepts A's link, or
--     both removing each other at once, serialise instead of deadlocking), then locks the rows it changes.
--   * the other side is never told and never sees the caller's block; the caller's block is never erased by the other side's removal.
--   * outcomes are RETURNED (ok / not_found / ...), not raised: the page can tell "already gone" from a bug. Bad input raises 22023.
--
-- Pair states (what the two rows say) and what the Diplomat must still do: the application may DELETE its own contact row directly
-- (it always could), which leaves a one-sided pair. The Diplomat functions therefore have to check BOTH rows ('accepted' on both sides).

-- Fix of the 0005 trigger function: it stamped revoked_at = now() (the START of its transaction), which can be EARLIER than the
-- granted_at of a grant committed by a transaction that started later but took the pair lock first -> ck_access_grants_revoked
-- (revoked_at >= granted_at) failed and the block was lost. GREATEST(now(), granted_at) is the same instant in every other case.
CREATE OR REPLACE FUNCTION civ.contact_left_accepted_revokes_grants() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = civ, pg_temp
    AS $$
BEGIN
    UPDATE civ.access_grants g SET revoked_at = GREATEST(now(), g.granted_at)
    WHERE g.user_id = NEW.user_id AND g.grantee_user_id = NEW.contact_user_id AND g.revoked_at IS NULL;
    RETURN NULL;
END
$$;
--;;

-- Revoke ONE open invite of the caller (revoke_invites revokes all of them). true = this call revoked it; false = there was nothing
-- to revoke (unknown, somebody else's, already used / revoked / expired). A concurrent accept_invite locks the invite row first, so
-- exactly one of the two wins.
CREATE FUNCTION civ.revoke_invite(p_invite_id uuid)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me uuid := civ.current_tenant_id();
    v_n  integer;
BEGIN
    IF p_invite_id IS NULL THEN
        RAISE EXCEPTION 'revoke_invite: invite id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    UPDATE civ.contact_invites i SET revoked_at = now()
    WHERE i.id = p_invite_id AND i.user_id = v_me
      AND i.used_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now();
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n > 0;
END
$$;
--;;

-- rename_contact: how the CALLER calls this contact in their own address book. -> ok | not_found
CREATE FUNCTION civ.rename_contact(p_contact_id uuid, p_alias text)
    RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me    uuid := civ.current_tenant_id();
    v_other uuid;
BEGIN
    IF p_contact_id IS NULL THEN
        RAISE EXCEPTION 'rename_contact: contact id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF NOT civ.is_clean_name(p_alias) THEN
        RAISE EXCEPTION 'rename_contact: alias must be one clean line of 1-40 characters' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT c.contact_user_id INTO v_other FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;   -- who to lock with
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    PERFORM civ.lock_pair(v_me, v_other);
    UPDATE civ.contacts c SET alias = p_alias WHERE c.id = p_contact_id AND c.user_id = v_me;   -- gone meanwhile -> no row
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    RETURN 'ok';
END
$$;
--;;

-- block_contact: the caller's row becomes 'blocked' and every grant the caller gave this person is revoked, whatever the previous
-- status was. Idempotent. The other side's row is not touched (they are not told). -> ok | not_found
CREATE FUNCTION civ.block_contact(p_contact_id uuid)
    RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me    uuid := civ.current_tenant_id();
    v_other uuid;
BEGIN
    IF p_contact_id IS NULL THEN
        RAISE EXCEPTION 'block_contact: contact id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT c.contact_user_id INTO v_other FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    PERFORM civ.lock_pair(v_me, v_other);
    PERFORM 1 FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    UPDATE civ.contacts c SET status = 'blocked' WHERE c.id = p_contact_id AND c.user_id = v_me AND c.status <> 'blocked';
    UPDATE civ.access_grants g SET revoked_at = GREATEST(now(), g.granted_at)
    WHERE g.user_id = v_me AND g.grantee_user_id = v_other AND g.revoked_at IS NULL;
    RETURN 'ok';
END
$$;
--;;

-- unblock_contact: 'blocked' -> 'accepted' again. It does NOT depend on (and so does not reveal) what the other side's row says, and it
-- gives nothing back: the grants were revoked by the block, access is default-deny again. Idempotent. -> ok | not_found
CREATE FUNCTION civ.unblock_contact(p_contact_id uuid)
    RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me    uuid := civ.current_tenant_id();
    v_other uuid;
BEGIN
    IF p_contact_id IS NULL THEN
        RAISE EXCEPTION 'unblock_contact: contact id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT c.contact_user_id INTO v_other FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    PERFORM civ.lock_pair(v_me, v_other);
    PERFORM 1 FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    UPDATE civ.contacts c SET status = 'accepted' WHERE c.id = p_contact_id AND c.user_id = v_me AND c.status = 'blocked';
    RETURN 'ok';
END
$$;
--;;

-- remove_contact: a clean break - the caller's row AND the other side's row go in one transaction (their grants in both directions
-- go with them: FK ON DELETE CASCADE), so nobody is left looking at a connection that is dead. Two things are never deleted:
--   * a BLOCKED contact (-> 'blocked': a block is the caller's decision to keep; unblock first if it should really go);
--   * the other side's row when THEY blocked the caller: their block is theirs, and is not erased by somebody else's removal.
-- The audit log is kept. A new invite can reconnect the two later. -> removed | not_found | blocked
CREATE FUNCTION civ.remove_contact(p_contact_id uuid)
    RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me     uuid := civ.current_tenant_id();
    v_other  uuid;
    v_status text;
BEGIN
    IF p_contact_id IS NULL THEN
        RAISE EXCEPTION 'remove_contact: contact id is required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT c.contact_user_id INTO v_other FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    PERFORM civ.lock_pair(v_me, v_other);
    SELECT c.status::text INTO v_status FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    IF v_status = 'blocked' THEN
        RETURN 'blocked';
    END IF;
    PERFORM 1 FROM civ.contacts c WHERE c.user_id = v_other AND c.contact_user_id = v_me FOR UPDATE;
    DELETE FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;
    DELETE FROM civ.contacts c WHERE c.user_id = v_other AND c.contact_user_id = v_me AND c.status <> 'blocked';
    RETURN 'removed';
END
$$;
--;;

-- set_grant: the caller allows (p_enabled) or stops allowing this contact to ask for p_scope. Default is deny. Idempotent.
--   enabling needs an ACCEPTED contact (-> not_accepted); stopping is always possible (a blocked contact has no grants anyway).
--   An active grant that has expired is closed and replaced, so "enabled" always means "in force now".
-- Done under the pair lock + the contact row lock, so it cannot interleave with block_contact / remove_contact (a plain INSERT could
-- slip a grant in just after a block). -> ok | not_found | not_accepted.   An unknown scope raises 22P02.
CREATE FUNCTION civ.set_grant(p_contact_id uuid, p_scope text, p_enabled boolean)
    RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
DECLARE
    v_me     uuid := civ.current_tenant_id();
    v_other  uuid;
    v_status text;
    v_scope  civ.acl_scope;
BEGIN
    IF p_contact_id IS NULL OR p_enabled IS NULL THEN
        RAISE EXCEPTION 'set_grant: contact id and enabled are required' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_scope := p_scope::civ.acl_scope;   -- 22P02 for a scope that does not exist
    SELECT c.contact_user_id INTO v_other FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    PERFORM civ.lock_pair(v_me, v_other);
    SELECT c.status::text INTO v_status FROM civ.contacts c WHERE c.id = p_contact_id AND c.user_id = v_me FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;

    IF NOT p_enabled THEN
        UPDATE civ.access_grants g SET revoked_at = GREATEST(now(), g.granted_at)
        WHERE g.user_id = v_me AND g.grantee_user_id = v_other AND g.scope = v_scope AND g.revoked_at IS NULL;
        RETURN 'ok';
    END IF;

    IF v_status <> 'accepted' THEN
        RETURN 'not_accepted';
    END IF;
    IF EXISTS (
        SELECT 1 FROM civ.access_grants g
        WHERE g.user_id = v_me AND g.grantee_user_id = v_other AND g.scope = v_scope AND g.revoked_at IS NULL
          AND (g.expires_at IS NULL OR g.expires_at > now())
    ) THEN
        RETURN 'ok';
    END IF;
    UPDATE civ.access_grants g SET revoked_at = GREATEST(now(), g.granted_at)   -- an expired, never-revoked row would block the unique "one active grant" index
    WHERE g.user_id = v_me AND g.grantee_user_id = v_other AND g.scope = v_scope AND g.revoked_at IS NULL;
    INSERT INTO civ.access_grants (user_id, grantee_user_id, scope) VALUES (v_me, v_other, v_scope);
    RETURN 'ok';
END
$$;
--;;

REVOKE ALL ON FUNCTION civ.revoke_invite(uuid) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.rename_contact(uuid, text) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.block_contact(uuid) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.unblock_contact(uuid) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.remove_contact(uuid) FROM PUBLIC;
--;;
REVOKE ALL ON FUNCTION civ.set_grant(uuid, text, boolean) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.revoke_invite(uuid) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.rename_contact(uuid, text) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.block_contact(uuid) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.unblock_contact(uuid) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.remove_contact(uuid) TO ai_civ_app;
--;;
GRANT EXECUTE ON FUNCTION civ.set_grant(uuid, text, boolean) TO ai_civ_app;
