-- 0020: smart inventory of consumables (v2.0, stage 4, PR 4.3).
--   * civ.inventory_items     - what ONE tenant keeps stock of (creatine, protein, omega-3, household chemicals ...): the canonical name, the remaining
--                               quantity in ONE unit, the daily burn rate (what the user said they use per day; 0 = no automatic burn and no reminders),
--                               how many days ahead to warn, and the bookkeeping of the daily job (last_burn_on = the last local day whose burn was applied,
--                               last_reminded_at / reminder_token = the open "time to buy" reminder).
--   * civ.inventory_movements - the AUDIT trail: every change of a quantity, append-only (the application can neither update nor delete a movement).
--                               The quantity of an item is the sum of its movements: every code path writes the movement and the new quantity together.
--   * civ.inventory_due_tenants - whom the daily Beat job must visit: one indexed query for ALL tenants (like cards_due_tenants).
-- The numbers come from the user's own words (the code verifies them); this schema refuses nonsense, foreign rows and a rewritten history.

CREATE TABLE civ.inventory_items (
    id                     uuid          NOT NULL DEFAULT gen_random_uuid(),
    user_id                uuid          NOT NULL,
    name                   text          NOT NULL,
    current_quantity       numeric(14,3) NOT NULL DEFAULT 0,
    unit                   text          NOT NULL,
    daily_burn_rate        numeric(12,3) NOT NULL DEFAULT 0,
    restock_threshold_days integer       NOT NULL DEFAULT 5,
    last_restocked_at      timestamptz,
    last_burn_on           date          NOT NULL DEFAULT (CURRENT_DATE - 1),
    last_reminded_at       timestamptz,
    reminder_token         uuid,
    created_at             timestamptz   NOT NULL DEFAULT now(),
    updated_at             timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_inventory_items PRIMARY KEY (id),
    CONSTRAINT uq_inventory_items_user_id_id UNIQUE (user_id, id),       -- target of composite tenant-safe FKs
    CONSTRAINT uq_inventory_items_user_name UNIQUE (user_id, name),
    CONSTRAINT fk_inventory_items_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_inventory_items_name CHECK (civ.is_clean_title(name)),
    CONSTRAINT ck_inventory_items_quantity CHECK (current_quantity >= 0 AND current_quantity <= 100000000),
    CONSTRAINT ck_inventory_items_unit CHECK (unit IN ('г', 'мл', 'порций', 'капсул', 'шт')),
    CONSTRAINT ck_inventory_items_burn CHECK (daily_burn_rate >= 0 AND daily_burn_rate <= 100000000),
    CONSTRAINT ck_inventory_items_threshold CHECK (restock_threshold_days BETWEEN 1 AND 90),
    CONSTRAINT ck_inventory_items_reminder CHECK (reminder_token IS NULL OR last_reminded_at IS NOT NULL)
);
--;;
ALTER TABLE civ.inventory_items ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.inventory_items AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- no DELETE for the application: an item with an audit trail is not erased from the chat
GRANT SELECT, INSERT, UPDATE ON civ.inventory_items TO ai_civ_app;
--;;

CREATE TABLE civ.inventory_movements (
    id                uuid          NOT NULL DEFAULT gen_random_uuid(),
    user_id           uuid          NOT NULL,
    item_id           uuid          NOT NULL,
    change_amount     numeric(14,3) NOT NULL,                 -- + restock / - consumption, in the unit of the item
    quantity_after    numeric(14,3) NOT NULL,
    reason            text          NOT NULL,
    source_message_id uuid,
    created_at        timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_inventory_movements PRIMARY KEY (id),
    CONSTRAINT fk_inventory_movements_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_inventory_movements_item FOREIGN KEY (user_id, item_id) REFERENCES civ.inventory_items (user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_inventory_movements_message FOREIGN KEY (user_id, source_message_id)
        REFERENCES civ.messages (user_id, id) ON DELETE SET NULL (source_message_id),
    CONSTRAINT ck_inventory_movements_change CHECK (change_amount <> 0),
    CONSTRAINT ck_inventory_movements_after CHECK (quantity_after >= 0),
    CONSTRAINT ck_inventory_movements_reason CHECK (reason IN ('daily_burn', 'manual_restock', 'correction'))
);
--;;
-- one message changes one item at most once: a replayed update finds its movement and changes nothing
CREATE UNIQUE INDEX uq_inventory_movements_message ON civ.inventory_movements (user_id, item_id, source_message_id) WHERE source_message_id IS NOT NULL;
--;;
CREATE INDEX ix_inventory_movements_user_item ON civ.inventory_movements (user_id, item_id, created_at);
--;;
ALTER TABLE civ.inventory_movements ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.inventory_movements AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- the audit trail is append-only
GRANT SELECT, INSERT ON civ.inventory_movements TO ai_civ_app;
--;;

-- Whom the daily job must visit: tenants (active group, onboarding done) whose LOCAL clock is at or past p_local_hour and that have an item with a burn rate
-- that either has a local day to burn (last_burn_on is older than the local "yesterday") or is running low (days left <= its threshold) and was not reminded
-- within p_cooldown. Returns tenant ids and nothing else; quiet hours and the claims are the worker's.
CREATE FUNCTION civ.inventory_due_tenants(p_limit integer, p_local_hour integer, p_cooldown interval)
    RETURNS TABLE (tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
    SELECT DISTINCT i.user_id
    FROM civ.inventory_items i
    JOIN civ.users u ON u.id = i.user_id
    WHERE u.group_state = 'active' AND u.onboarding_state = 'done' AND u.forum_chat_id IS NOT NULL
      AND i.daily_burn_rate > 0
      AND EXTRACT(hour FROM (now() AT TIME ZONE u.timezone)) >= p_local_hour
      AND (
            i.last_burn_on < ((now() AT TIME ZONE u.timezone)::date - 1)
         OR (i.current_quantity / i.daily_burn_rate <= i.restock_threshold_days
             AND (i.last_reminded_at IS NULL OR i.last_reminded_at < now() - p_cooldown))
      )
    LIMIT GREATEST(p_limit, 1)
$$;
--;;
REVOKE ALL ON FUNCTION civ.inventory_due_tenants(integer, integer, interval) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.inventory_due_tenants(integer, integer, interval) TO ai_civ_app;
