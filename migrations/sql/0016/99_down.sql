-- 0016 / downgrade
DROP VIEW IF EXISTS civ.character_progress;
--;;
DROP FUNCTION IF EXISTS civ.award_xp(text, text, integer);
--;;
DROP TABLE IF EXISTS civ.xp_ledger;
--;;
DROP FUNCTION IF EXISTS civ.xp_ledger_daily_cap();
--;;
DROP FUNCTION IF EXISTS civ.xp_on_local_day(uuid, timestamptz);
--;;
DROP TABLE IF EXISTS civ.character_profiles;
