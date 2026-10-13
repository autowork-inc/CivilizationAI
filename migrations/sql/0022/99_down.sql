-- 0022 / downgrade
DROP FUNCTION IF EXISTS civ.time_capsules_due_tenants(integer, interval);
--;;
DROP TABLE IF EXISTS civ.time_capsules;
--;;
DROP FUNCTION IF EXISTS civ.time_capsules_guard();
