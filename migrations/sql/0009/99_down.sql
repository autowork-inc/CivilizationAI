-- 0009 / downgrade
ALTER TABLE civ.personas DROP CONSTRAINT IF EXISTS ck_personas_title_source;
--;;
ALTER TABLE civ.personas DROP COLUMN IF EXISTS title_source;
--;;
ALTER TABLE civ.personas DROP COLUMN IF EXISTS title_changed_at;
