-- 0009: who named a topic last, and when. The bot may rename a topic on its own (v1.1), but not on every message: the claim for a
-- rename is taken in this row (compare-and-set under FOR UPDATE), so two processes / two quick messages cannot both rename.
-- title_changed_at NULL = never renamed since the migration: such a topic is eligible at once. A topic the bot creates is stamped when it is
-- bound to its forum topic (the name was just chosen). title_source = who set the CURRENT name: the bot, or the user by hand (a manual
-- name is respected for longer).
ALTER TABLE civ.personas ADD COLUMN title_changed_at timestamptz;
--;;
ALTER TABLE civ.personas ADD COLUMN title_source text NOT NULL DEFAULT 'bot';
--;;
ALTER TABLE civ.personas ADD CONSTRAINT ck_personas_title_source CHECK (title_source IN ('bot', 'user'));
