-- 0011: what the user attached to a message (a photo, a PDF, a TXT or DOCX file). The BYTES are never stored: they are downloaded into memory,
-- handed to the model for this one reply and dropped. What stays is a record of the fact (kind, size, a hash, Telegram's file id) and, for
-- the two text formats we read ourselves, the extracted text (capped), so that a later feature can work with the document again.
-- One attachment per message (an album arrives as separate messages); the row hangs on the user's message (composite FK, same tenant).
CREATE TABLE civ.message_attachments (
    id               uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id          uuid        NOT NULL,
    message_id       uuid        NOT NULL,
    kind             text        NOT NULL,                      -- what Telegram called it: photo | document
    content_kind     text        NOT NULL,                      -- what the bot made of it: image | pdf | txt | docx
    mime_type        text        NOT NULL,
    file_name        text,                                      -- one clean line, as the sender named the file (NULL for photos)
    size_bytes       integer     NOT NULL,
    sha256           text        NOT NULL,                      -- of the downloaded bytes (lower-case hex)
    telegram_file_id text        NOT NULL,
    extracted_text   text,                                      -- txt / docx only
    text_truncated   boolean     NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_message_attachments PRIMARY KEY (id),
    CONSTRAINT fk_message_attachments_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_message_attachments_message FOREIGN KEY (user_id, message_id)
        REFERENCES civ.messages (user_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_message_attachments_message UNIQUE (user_id, message_id),
    CONSTRAINT ck_message_attachments_kind CHECK (kind IN ('photo', 'document')),
    CONSTRAINT ck_message_attachments_content_kind CHECK (content_kind IN ('image', 'pdf', 'txt', 'docx')),
    CONSTRAINT ck_message_attachments_size CHECK (size_bytes > 0),
    CONSTRAINT ck_message_attachments_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_message_attachments_name CHECK (file_name IS NULL OR (char_length(file_name) BETWEEN 1 AND 255 AND file_name !~ '[[:cntrl:]]')),
    CONSTRAINT ck_message_attachments_text CHECK (
        (content_kind IN ('txt', 'docx') AND extracted_text IS NOT NULL AND char_length(extracted_text) <= 200000)
        OR (content_kind IN ('image', 'pdf') AND extracted_text IS NULL AND NOT text_truncated)
    )
);
--;;
ALTER TABLE civ.message_attachments ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.message_attachments AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- no UPDATE: a record of what was received does not change
GRANT SELECT, INSERT, DELETE ON civ.message_attachments TO ai_civ_app;
