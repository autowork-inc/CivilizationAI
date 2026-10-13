-- 0011 / downgrade: the records of attachments go (the messages stay; the bytes were never stored)
DROP TABLE IF EXISTS civ.message_attachments;
