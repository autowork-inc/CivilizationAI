-- 0025 / downgrade. Tenants that were already moved stay where they are (the data was never deleted).
DROP FUNCTION IF EXISTS civ.relocate_group(bigint, bigint);
