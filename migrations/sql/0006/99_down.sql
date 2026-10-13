-- 0006 / downgrade
DROP FUNCTION IF EXISTS civ.set_grant(uuid, text, boolean);
--;;
DROP FUNCTION IF EXISTS civ.remove_contact(uuid);
--;;
DROP FUNCTION IF EXISTS civ.unblock_contact(uuid);
--;;
DROP FUNCTION IF EXISTS civ.block_contact(uuid);
--;;
DROP FUNCTION IF EXISTS civ.rename_contact(uuid, text);
--;;
DROP FUNCTION IF EXISTS civ.revoke_invite(uuid);
--;;
CREATE OR REPLACE FUNCTION civ.contact_left_accepted_revokes_grants() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = civ, pg_temp
    AS $$
BEGIN
    UPDATE civ.access_grants g SET revoked_at = now()
    WHERE g.user_id = NEW.user_id AND g.grantee_user_id = NEW.contact_user_id AND g.revoked_at IS NULL;
    RETURN NULL;
END
$$;
