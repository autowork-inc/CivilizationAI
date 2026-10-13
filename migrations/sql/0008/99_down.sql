-- 0008 / downgrade
DROP FUNCTION IF EXISTS civ.purge_events(integer, integer);
--;;
DROP FUNCTION IF EXISTS civ.respond_event_proposal(uuid, boolean);
--;;
DROP FUNCTION IF EXISTS civ.propose_event(uuid, text, timestamptz, timestamptz);
--;;
DROP TABLE IF EXISTS civ.event_proposals;
--;;
DROP TABLE IF EXISTS civ.event_drafts;
--;;
-- nothing in the code will deliver these any more
DELETE FROM civ.notification_outbox WHERE kind IN ('event_proposal', 'event_proposal_answer');
--;;
-- the meetings stay in the calendars; only their source label goes back to the closed pair of values
UPDATE civ.events SET created_via = 'bot_confirmed' WHERE created_via = 'peer_proposal';
--;;
ALTER TABLE civ.events DROP CONSTRAINT ck_events_created_via;
--;;
ALTER TABLE civ.events ADD CONSTRAINT ck_events_created_via CHECK (created_via IN ('mini_app', 'bot_confirmed'));
--;;
DROP FUNCTION IF EXISTS civ.is_clean_title(text);
