-- 0019: recipe generator (v2.0, stage 4, PR 4.2).
--   * civ.recipe_history - every recipe the bot suggested to ONE tenant: a clean title, the ingredients (a JSON array of strings), the numbered
--                          steps, the preparation time and the focus of the nutrition. status 'suggested' -> 'cooked' (the user pressed the button) and
--                          never back: a cooked recipe is final (trigger). The last 5 recipes of the last 14 days are what a new suggestion must not repeat.
--   * source_message_id (the user's message that asked) is unique per tenant: a replayed update cannot suggest - or pay - twice.
-- The words come from the model, the code validates them before the insert; this schema refuses nonsense, foreign rows and a rewritten history.

CREATE TABLE civ.recipe_history (
    id                uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id           uuid        NOT NULL,
    title             text        NOT NULL,
    prep_time_minutes smallint    NOT NULL,
    ingredients_used  jsonb       NOT NULL,
    instructions      text        NOT NULL,
    nutrition_focus   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    status            text        NOT NULL DEFAULT 'suggested',
    cooked_at         timestamptz,
    source_message_id uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_recipe_history PRIMARY KEY (id),
    CONSTRAINT uq_recipe_history_user_id_id UNIQUE (user_id, id),       -- target of composite tenant-safe FKs
    CONSTRAINT fk_recipe_history_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_recipe_history_message FOREIGN KEY (user_id, source_message_id)
        REFERENCES civ.messages (user_id, id) ON DELETE SET NULL (source_message_id),
    CONSTRAINT ck_recipe_history_title CHECK (civ.is_clean_title(title)),
    CONSTRAINT ck_recipe_history_prep CHECK (prep_time_minutes BETWEEN 1 AND 600),
    CONSTRAINT ck_recipe_history_ingredients CHECK (jsonb_typeof(ingredients_used) = 'array' AND jsonb_array_length(ingredients_used) BETWEEN 1 AND 30),
    CONSTRAINT ck_recipe_history_instructions CHECK (btrim(instructions) <> '' AND char_length(instructions) <= 4000),
    CONSTRAINT ck_recipe_history_focus CHECK (jsonb_typeof(nutrition_focus) = 'array' AND jsonb_array_length(nutrition_focus) <= 4),
    CONSTRAINT ck_recipe_history_status CHECK (status IN ('suggested', 'cooked')),
    CONSTRAINT ck_recipe_history_cooked CHECK ((status = 'cooked') = (cooked_at IS NOT NULL))
);
--;;
CREATE UNIQUE INDEX uq_recipe_history_message ON civ.recipe_history (user_id, source_message_id) WHERE source_message_id IS NOT NULL;
--;;
CREATE INDEX ix_recipe_history_user_created ON civ.recipe_history (user_id, created_at);
--;;
-- a cooked recipe is final: it does not go back to 'suggested' and cannot be cooked (paid) a second time
CREATE FUNCTION civ.recipe_history_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.status = 'cooked' THEN
        RAISE EXCEPTION 'recipe_history: a cooked recipe is final' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;
--;;
CREATE TRIGGER recipe_history_guard BEFORE UPDATE ON civ.recipe_history FOR EACH ROW EXECUTE FUNCTION civ.recipe_history_guard();
--;;
ALTER TABLE civ.recipe_history ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.recipe_history AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- the words of a recipe are never edited; only the status and its timestamp move (the guard above makes that one-way)
GRANT SELECT, INSERT, DELETE ON civ.recipe_history TO ai_civ_app;
--;;
GRANT UPDATE (status, cooked_at) ON civ.recipe_history TO ai_civ_app;
