-- 0021: geo notes and places (v2.0, stage 4, PR 4.4).
--   * civ.places     - the places ONE tenant has named (home, gym, office, shop ...): a clean name, its synonyms, the centre (latitude / longitude, 6 decimals)
--                      and the radius in metres inside which a sent location counts as "being there". The distance is computed by the code (haversine, no GIS).
--   * civ.geo_notes  - a note that belongs to a place: place_id when the place is known, otherwise place_name (the name the user gave; the note is bound to the
--                      place the first time that place is saved), and/or its own point (latitude / longitude: a note made on a location). A note is shown when the
--                      user sends a location inside the place (or near the point); "done" archives it. notified_at = the last time it was shown.
-- The words and the numbers come from the user; the code validates them. This schema refuses nonsense and foreign rows.

CREATE TABLE civ.places (
    id            uuid          NOT NULL DEFAULT gen_random_uuid(),
    user_id       uuid          NOT NULL,
    name          text          NOT NULL,
    aliases       text[]        NOT NULL DEFAULT '{}',
    latitude      numeric(9,6)  NOT NULL,
    longitude     numeric(9,6)  NOT NULL,
    radius_meters integer       NOT NULL DEFAULT 150,
    created_at    timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT pk_places PRIMARY KEY (id),
    CONSTRAINT uq_places_user_id_id UNIQUE (user_id, id),       -- target of composite tenant-safe FKs
    CONSTRAINT uq_places_user_name UNIQUE (user_id, name),
    CONSTRAINT fk_places_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_places_name CHECK (civ.is_clean_title(name)),
    CONSTRAINT ck_places_aliases CHECK (civ.exercise_aliases_valid(aliases)),
    CONSTRAINT ck_places_latitude CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT ck_places_longitude CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT ck_places_radius CHECK (radius_meters BETWEEN 10 AND 5000)
);
--;;
ALTER TABLE civ.places ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.places AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.places TO ai_civ_app;
--;;

CREATE TABLE civ.geo_notes (
    id                uuid          NOT NULL DEFAULT gen_random_uuid(),
    user_id           uuid          NOT NULL,
    place_id          uuid,
    place_name        text,
    latitude          numeric(9,6),
    longitude         numeric(9,6),
    content           text          NOT NULL,
    status            text          NOT NULL DEFAULT 'active',
    created_at        timestamptz   NOT NULL DEFAULT now(),
    notified_at       timestamptz,
    archived_at       timestamptz,
    source_message_id uuid,
    CONSTRAINT pk_geo_notes PRIMARY KEY (id),
    CONSTRAINT uq_geo_notes_user_id_id UNIQUE (user_id, id),
    CONSTRAINT fk_geo_notes_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    -- deleting a place keeps its notes (they keep place_name and are bound again when the place is saved again)
    CONSTRAINT fk_geo_notes_place FOREIGN KEY (user_id, place_id) REFERENCES civ.places (user_id, id) ON DELETE SET NULL (place_id),
    CONSTRAINT fk_geo_notes_message FOREIGN KEY (user_id, source_message_id)
        REFERENCES civ.messages (user_id, id) ON DELETE SET NULL (source_message_id),
    CONSTRAINT ck_geo_notes_content CHECK (btrim(content) <> '' AND char_length(content) <= 500 AND content !~ E'[\\x00-\\x1f\\x7f\\u202a-\\u202e\\u2066-\\u2069]'),
    CONSTRAINT ck_geo_notes_place_name CHECK (place_name IS NULL OR civ.is_clean_title(place_name)),
    CONSTRAINT ck_geo_notes_status CHECK (status IN ('active', 'archived')),
    CONSTRAINT ck_geo_notes_point CHECK ((latitude IS NULL) = (longitude IS NULL) AND (latitude IS NULL OR (latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180))),
    CONSTRAINT ck_geo_notes_anchor CHECK (place_id IS NOT NULL OR place_name IS NOT NULL OR latitude IS NOT NULL),
    CONSTRAINT ck_geo_notes_archived CHECK ((status = 'archived') = (archived_at IS NOT NULL))
);
--;;
CREATE INDEX ix_geo_notes_user_status ON civ.geo_notes (user_id, status);
--;;
CREATE INDEX ix_geo_notes_user_place ON civ.geo_notes (user_id, place_id);
--;;
CREATE INDEX ix_geo_notes_user_message ON civ.geo_notes (user_id, source_message_id);
--;;
ALTER TABLE civ.geo_notes ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.geo_notes AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.geo_notes TO ai_civ_app;
