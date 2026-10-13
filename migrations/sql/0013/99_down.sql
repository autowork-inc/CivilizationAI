-- 0013 / downgrade
ALTER TABLE civ.event_drafts DROP COLUMN IF EXISTS task_id;
--;;
DROP TABLE IF EXISTS civ.task_events;
--;;
DROP TABLE IF EXISTS civ.tasks;
--;;
DROP FUNCTION IF EXISTS civ.tasks_guard_depth();
