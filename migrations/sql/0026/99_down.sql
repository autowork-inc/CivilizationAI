-- 0026 / downgrade. People without a Telegram id cannot exist in the old schema (telegram_user_id is NOT NULL again), so they are deleted
-- (their rows go with them: every tenant table cascades). Telegram tenants are untouched.
DROP FUNCTION IF EXISTS civ.publish_app_notification(text, jsonb, text);
--;;
DROP FUNCTION IF EXISTS civ.resolve_tenant_by_email(text);
--;;
DROP FUNCTION IF EXISTS civ.register_app_user(text, text, text);
--;;
DROP TABLE IF EXISTS civ.user_sessions;
--;;
DROP INDEX IF EXISTS civ.uq_messages_user_reply_to;
--;;
DROP INDEX IF EXISTS civ.uq_messages_user_client_id;
--;;
ALTER TABLE civ.messages
    DROP CONSTRAINT IF EXISTS ck_messages_reply_not_self,
    DROP CONSTRAINT IF EXISTS fk_messages_reply_to;
--;;
ALTER TABLE civ.messages
    DROP COLUMN IF EXISTS cards,
    DROP COLUMN IF EXISTS reply_to_id,
    DROP COLUMN IF EXISTS client_message_id;
--;;
DELETE FROM civ.users WHERE telegram_user_id IS NULL;
--;;
DROP INDEX IF EXISTS civ.uq_users_external_auth;
--;;
DROP INDEX IF EXISTS civ.uq_users_email_lower;
--;;
ALTER TABLE civ.users
    DROP CONSTRAINT IF EXISTS ck_users_pass_hash,
    DROP CONSTRAINT IF EXISTS ck_users_external,
    DROP CONSTRAINT IF EXISTS ck_users_email,
    DROP CONSTRAINT IF EXISTS ck_users_identity;
--;;
ALTER TABLE civ.users
    DROP COLUMN IF EXISTS external_auth_id,
    DROP COLUMN IF EXISTS external_auth_provider,
    DROP COLUMN IF EXISTS pass_hash,
    DROP COLUMN IF EXISTS email;
--;;
ALTER TABLE civ.users ALTER COLUMN telegram_user_id SET NOT NULL;
