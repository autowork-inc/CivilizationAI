-- 0015 / downgrade
ALTER TABLE civ.personas DROP CONSTRAINT IF EXISTS ck_personas_mode;
--;;
ALTER TABLE civ.personas DROP COLUMN IF EXISTS mode;
