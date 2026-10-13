-- 0001 / 04 - Tier 3 source of truth (messages), Outbox, Tier 2 (episodic memory + pgvector).

-- Every message is written here FIRST (PostgreSQL is the source of truth); Redis is only a rebuildable cache.
CREATE TABLE civ.messages (
    id                  uuid             NOT NULL DEFAULT gen_random_uuid(),
    user_id             uuid             NOT NULL,
    persona_id          uuid,
    role                civ.message_role NOT NULL,
    content             text             NOT NULL,
    telegram_message_id bigint,
    created_at          timestamptz      NOT NULL DEFAULT now(),
    CONSTRAINT pk_messages PRIMARY KEY (id),
    CONSTRAINT fk_messages_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_messages_persona FOREIGN KEY (user_id, persona_id)
        REFERENCES civ.personas (user_id, id) ON DELETE SET NULL (persona_id),
    -- a replayed Telegram update cannot create a duplicate row
    CONSTRAINT uq_messages_user_telegram UNIQUE (user_id, telegram_message_id)
);
--;;
CREATE INDEX ix_messages_user_created ON civ.messages (user_id, created_at);
--;;
CREATE INDEX ix_messages_user_persona_created ON civ.messages (user_id, persona_id, created_at);
--;;

-- ============ Outbox: memory work that must survive crashes ============
-- A row is NOT deleted on success: it becomes status='done' (HTTP 200 / commit confirmed) and a separate
-- Celery task purges 'done' rows older than the TTL (7 days).
-- Claiming (per tenant, under RLS):
--   SELECT ... FROM civ.memory_outbox
--   WHERE status IN ('pending','failed','processing') AND attempts < max_attempts
--     AND (locked_until IS NULL OR locked_until < now())
--   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1;
-- then SET status='processing', attempts=attempts+1, locked_until=now()+lease. A crashed worker's lease simply
-- expires, so a 'processing' row is picked up again; rows out of attempts are moved to 'dead' for manual review.
CREATE TABLE civ.memory_outbox (
    id              bigint             GENERATED ALWAYS AS IDENTITY,
    user_id         uuid               NOT NULL,
    operation       civ.outbox_operation NOT NULL,
    payload         jsonb              NOT NULL,
    idempotency_key text               NOT NULL,
    status          civ.outbox_status  NOT NULL DEFAULT 'pending',
    attempts        smallint           NOT NULL DEFAULT 0,
    max_attempts    smallint           NOT NULL DEFAULT 5,
    locked_until    timestamptz,
    last_error      text,
    created_at      timestamptz        NOT NULL DEFAULT now(),
    processed_at    timestamptz,
    CONSTRAINT pk_memory_outbox PRIMARY KEY (id),
    CONSTRAINT fk_memory_outbox_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_memory_outbox_user_idem UNIQUE (user_id, idempotency_key),
    CONSTRAINT ck_memory_outbox_attempts CHECK (attempts >= 0 AND max_attempts >= 1),
    CONSTRAINT ck_memory_outbox_terminal CHECK ((status IN ('done', 'dead')) = (processed_at IS NOT NULL))
);
--;;
CREATE INDEX ix_memory_outbox_open ON civ.memory_outbox (created_at)
    WHERE status IN ('pending', 'processing', 'failed');
--;;
CREATE INDEX ix_memory_outbox_done_ttl ON civ.memory_outbox (processed_at) WHERE status = 'done';
--;;

-- ============ Tier 2: episodic memory (RAG) ============
-- Same database => writing an episode, closing its outbox task and filing pending_facts is ONE transaction.
-- Embedding model: Gemini gemini-embedding-001 asked for 1536 dims (output_dimensionality; the native 3072 does not fit an HNSW index). Switching models = new column/table + re-embedding
-- (embedding_model records which model produced each vector).
CREATE TABLE civ.episodic_memories (
    id                 uuid               NOT NULL DEFAULT gen_random_uuid(),
    user_id            uuid               NOT NULL,
    persona_id         uuid,
    idempotency_key    text               NOT NULL,   -- upsert key: a retried task overwrites, never duplicates
    kind               civ.episode_kind   NOT NULL,
    content            text               NOT NULL,   -- what the LLM-Judge and the prompt actually see
    embedding          vector(1536)       NOT NULL,
    embedding_model    text               NOT NULL,
    occurred_from      timestamptz        NOT NULL,
    occurred_to        timestamptz        NOT NULL,
    source_message_ids uuid[]             NOT NULL DEFAULT '{}',
    importance         smallint           NOT NULL DEFAULT 5,
    sensitivity        civ.sensitivity    NOT NULL DEFAULT 'normal',  -- 'private'/'secret' never leave the tenant
    created_at         timestamptz        NOT NULL DEFAULT now(),
    CONSTRAINT pk_episodic_memories PRIMARY KEY (id),
    CONSTRAINT fk_episodic_memories_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_episodic_memories_persona FOREIGN KEY (user_id, persona_id)
        REFERENCES civ.personas (user_id, id) ON DELETE SET NULL (persona_id),
    CONSTRAINT uq_episodic_memories_user_idem UNIQUE (user_id, idempotency_key),
    CONSTRAINT ck_episodic_memories_importance CHECK (importance BETWEEN 1 AND 10),
    CONSTRAINT ck_episodic_memories_period CHECK (occurred_to >= occurred_from)
);
--;;
CREATE INDEX ix_episodic_memories_user_time ON civ.episodic_memories (user_id, occurred_from);
--;;
CREATE INDEX ix_episodic_memories_user_persona ON civ.episodic_memories (user_id, persona_id);
--;;
-- Queries always filter by user_id. With pgvector >= 0.8.0 use: SET LOCAL hnsw.iterative_scan = relaxed_order;
-- so that a filtered HNSW scan does not return fewer rows than requested.
CREATE INDEX ix_episodic_memories_embedding_hnsw ON civ.episodic_memories
    USING hnsw (embedding vector_cosine_ops);
