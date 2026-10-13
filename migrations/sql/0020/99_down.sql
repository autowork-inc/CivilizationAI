-- 0020 / downgrade
DROP FUNCTION IF EXISTS civ.inventory_due_tenants(integer, integer, interval);
--;;
DROP TABLE IF EXISTS civ.inventory_movements;
--;;
DROP TABLE IF EXISTS civ.inventory_items;
