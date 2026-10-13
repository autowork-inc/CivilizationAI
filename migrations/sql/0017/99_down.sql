-- 0017 / downgrade
DROP FUNCTION IF EXISTS civ.send_cheer(uuid, text);
--;;
DROP FUNCTION IF EXISTS civ.xp_grant(uuid, text, text, integer);
--;;
DROP FUNCTION IF EXISTS civ.tavern_avatar(uuid);
--;;
DROP FUNCTION IF EXISTS civ.tavern_feed();
--;;
DROP FUNCTION IF EXISTS civ.tavern_authorize(uuid);
--;;
DROP TABLE IF EXISTS civ.tavern_cheers;
