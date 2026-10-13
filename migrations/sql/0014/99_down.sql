-- 0014 / downgrade
DROP FUNCTION IF EXISTS civ.cards_due_tenants(integer, interval);
--;;
DROP TABLE IF EXISTS civ.card_reviews;
--;;
DROP TABLE IF EXISTS civ.cards;
--;;
DROP FUNCTION IF EXISTS civ.card_options_valid(text[]);
