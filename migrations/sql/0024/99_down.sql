-- 0024 / downgrade. The mood estimates are derived data (they can be recomputed from messages); the check-in preference returns to the system default.
DROP TABLE IF EXISTS civ.daily_mood;
--;;
ALTER TABLE civ.users DROP CONSTRAINT IF EXISTS ck_users_checkin_per_day;
--;;
ALTER TABLE civ.users DROP COLUMN IF EXISTS checkin_per_day;
