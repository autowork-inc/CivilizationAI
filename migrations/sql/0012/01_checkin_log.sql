-- 0012: the log of proactive check-ins ("the user has been silent, the bot writes first"). One row per (tenant, local day, slot): the row IS the claim
-- (INSERT ... ON CONFLICT DO NOTHING), so overlapping Beat ticks and retries cannot fire one slot twice. The same rows tell how many check-ins went
-- unanswered (sent after the user's last message): after K of them the bot stops writing first until the user writes.
CREATE TABLE civ.proactive_checkin_log (
    id         uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id    uuid        NOT NULL,
    slot_date  date        NOT NULL,                       -- the tenant-local day
    slot_time  time        NOT NULL,                       -- the tenant-local time of the slot
    sent_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_proactive_checkin_log PRIMARY KEY (id),
    CONSTRAINT fk_proactive_checkin_log_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_proactive_checkin_log_slot UNIQUE (user_id, slot_date, slot_time)
);
--;;
CREATE INDEX ix_proactive_checkin_log_user_sent ON civ.proactive_checkin_log (user_id, sent_at);
--;;
ALTER TABLE civ.proactive_checkin_log ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.proactive_checkin_log AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- no UPDATE: a claim is made or given back (DELETE), never edited
GRANT SELECT, INSERT, DELETE ON civ.proactive_checkin_log TO ai_civ_app;
