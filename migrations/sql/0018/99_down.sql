-- 0018 / downgrade
DROP TABLE IF EXISTS civ.workout_sets;
--;;
DROP TABLE IF EXISTS civ.workout_sessions;
--;;
DROP TABLE IF EXISTS civ.exercises;
--;;
DROP FUNCTION IF EXISTS civ.exercise_aliases_valid(text[]);
