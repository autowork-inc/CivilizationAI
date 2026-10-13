-- 0015: the language mode of a topic (v2.0, PR 3.3). 'normal' = the bot answers in the user's language; 'immersion_da' = Danish immersion
-- (A2-B1, gentle corrections). The mode belongs to the persona (a topic), so one topic can be Danish while the others stay as they are.
ALTER TABLE civ.personas ADD COLUMN mode text NOT NULL DEFAULT 'normal';
--;;
ALTER TABLE civ.personas ADD CONSTRAINT ck_personas_mode CHECK (mode IN ('normal', 'immersion_da'));
