-- 0004 / downgrade
DROP FUNCTION IF EXISTS civ.register_tenant(bigint, bigint);
--;;
ALTER TABLE civ.users
    DROP CONSTRAINT IF EXISTS ck_users_group_state,
    DROP COLUMN IF EXISTS group_state,
    DROP COLUMN IF EXISTS group_state_changed_at;
