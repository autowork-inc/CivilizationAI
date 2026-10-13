-- 0003 / downgrade
DROP TABLE IF EXISTS civ.proactive_schedule;
--;;
ALTER TABLE civ.users
    DROP COLUMN IF EXISTS onboarding_state,
    DROP COLUMN IF EXISTS onboarding_step,
    DROP COLUMN IF EXISTS onboarding_draft,
    DROP COLUMN IF EXISTS onboarding_completed_at,
    DROP COLUMN IF EXISTS quiet_hours_start,
    DROP COLUMN IF EXISTS quiet_hours_end;
