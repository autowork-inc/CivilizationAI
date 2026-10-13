-- 0014: spaced repetition (v2.0, stage 3, PR 3.2).
--   * civ.cards        - a multiple-choice card: a question, EXACTLY four distinct options, the index of the right one, an explanation. The model
--                        proposes the words, the code validates them, and this table refuses anything else (CHECKs below).
--   * civ.card_reviews - the SM-2 state of ONE card (one row per card): state / interval / ease / reps, when it is due again, when it was
--                        last answered, when it was last SENT to the chat and which option the user chose. A card is "awaiting an answer" while
--                        last_sent_at > reviewed_at (or it was never answered): a button can only answer a card the bot has actually sent.
--   * civ.cards_due_tenants - which tenants have a card to send: one indexed query for the Beat dispatcher (like pending_notification_tenants).

-- 4 non-empty options, no control characters, each at most 100 characters, pairwise different (case and surrounding spaces do not count).
CREATE FUNCTION civ.card_options_valid(p_options text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT p_options IS NOT NULL
       AND array_ndims(p_options) = 1
       AND cardinality(p_options) = 4
       AND (SELECT bool_and(o IS NOT NULL AND btrim(o) <> '' AND char_length(o) <= 100 AND o !~ '[\x01-\x1f\x7f]') FROM unnest(p_options) AS o)
       AND (SELECT count(DISTINCT lower(btrim(o))) FROM unnest(p_options) AS o) = 4
$$;
--;;
CREATE TABLE civ.cards (
    id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    user_id       uuid        NOT NULL,
    deck          text        NOT NULL,
    question      text        NOT NULL,
    options       text[]      NOT NULL,
    correct_index smallint    NOT NULL,
    explanation   text        NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_cards PRIMARY KEY (id),
    CONSTRAINT uq_cards_user_id_id UNIQUE (user_id, id),       -- target of composite tenant-safe FKs
    CONSTRAINT fk_cards_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_cards_deck CHECK (civ.is_clean_title(deck)),
    CONSTRAINT ck_cards_question CHECK (btrim(question) <> '' AND char_length(question) <= 400 AND question !~ '[\x01-\x1f\x7f]'),
    CONSTRAINT ck_cards_options CHECK (civ.card_options_valid(options)),
    CONSTRAINT ck_cards_correct_index CHECK (correct_index BETWEEN 0 AND 3),
    CONSTRAINT ck_cards_explanation CHECK (char_length(explanation) <= 600)
);
--;;
-- the same question is not stored twice in a deck
CREATE UNIQUE INDEX uq_cards_user_deck_question ON civ.cards (user_id, deck, question);
--;;
CREATE INDEX ix_cards_user_deck ON civ.cards (user_id, deck);
--;;
ALTER TABLE civ.cards ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.cards AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
-- a card is made or removed, never edited (a wrong card is deleted and made again)
GRANT SELECT, INSERT, DELETE ON civ.cards TO ai_civ_app;
--;;

CREATE TABLE civ.card_reviews (
    id             uuid         NOT NULL DEFAULT gen_random_uuid(),
    user_id        uuid         NOT NULL,
    card_id        uuid         NOT NULL,
    state          text         NOT NULL DEFAULT 'new',          -- new (never answered) | learning (fewer than 2 correct in a row) | review
    interval_days  integer      NOT NULL DEFAULT 0,
    ease           numeric(4,2) NOT NULL DEFAULT 2.50,           -- SM-2 easiness factor
    reps           integer      NOT NULL DEFAULT 0,              -- consecutive correct answers
    lapses         integer      NOT NULL DEFAULT 0,
    next_review_at timestamptz  NOT NULL DEFAULT now(),
    reviewed_at    timestamptz,                                  -- the last answer
    last_sent_at   timestamptz,                                  -- the last time the bot sent the card to the chat
    last_chosen    smallint,                                     -- the option of the last answer
    CONSTRAINT pk_card_reviews PRIMARY KEY (id),
    CONSTRAINT uq_card_reviews_card UNIQUE (user_id, card_id),
    CONSTRAINT fk_card_reviews_user FOREIGN KEY (user_id) REFERENCES civ.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_card_reviews_card FOREIGN KEY (user_id, card_id) REFERENCES civ.cards (user_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_card_reviews_state CHECK (state IN ('new', 'learning', 'review')),
    CONSTRAINT ck_card_reviews_interval CHECK (interval_days BETWEEN 0 AND 3650),
    CONSTRAINT ck_card_reviews_ease CHECK (ease BETWEEN 1.30 AND 3.50),
    CONSTRAINT ck_card_reviews_counts CHECK (reps >= 0 AND lapses >= 0),
    CONSTRAINT ck_card_reviews_chosen CHECK (last_chosen IS NULL OR last_chosen BETWEEN 0 AND 3),
    CONSTRAINT ck_card_reviews_answered CHECK ((state = 'new') = (reviewed_at IS NULL) AND (reviewed_at IS NULL) = (last_chosen IS NULL))
);
--;;
CREATE INDEX ix_card_reviews_user_due ON civ.card_reviews (user_id, next_review_at);
--;;
CREATE INDEX ix_card_reviews_user_sent ON civ.card_reviews (user_id, last_sent_at);
--;;
ALTER TABLE civ.card_reviews ENABLE ROW LEVEL SECURITY;
--;;
CREATE POLICY tenant_isolation ON civ.card_reviews AS PERMISSIVE FOR ALL TO ai_civ_app
    USING (user_id = (SELECT civ.current_tenant_id()))
    WITH CHECK (user_id = (SELECT civ.current_tenant_id()));
--;;
GRANT SELECT, INSERT, UPDATE, DELETE ON civ.card_reviews TO ai_civ_app;
--;;

-- Whom the dispatcher must visit: tenants (active group, onboarding done) with at least one card that has to be sent now: it is due, and it was
-- either never sent since it became due or its last send is older than p_resend (an unanswered card is offered again after that long).
-- Returns tenant ids and nothing else; the quiet hours and the daily limit are decided per tenant by the worker.
CREATE FUNCTION civ.cards_due_tenants(p_limit integer, p_resend interval)
    RETURNS TABLE (tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = civ, pg_temp
    AS $$
    SELECT DISTINCT r.user_id
    FROM civ.card_reviews r
    JOIN civ.users u ON u.id = r.user_id
    WHERE r.next_review_at <= now()
      AND (r.last_sent_at IS NULL OR r.last_sent_at < r.next_review_at OR r.last_sent_at < now() - p_resend)
      AND u.group_state = 'active' AND u.onboarding_state = 'done' AND u.forum_chat_id IS NOT NULL
    LIMIT GREATEST(p_limit, 1)
$$;
--;;
REVOKE ALL ON FUNCTION civ.cards_due_tenants(integer, interval) FROM PUBLIC;
--;;
GRANT EXECUTE ON FUNCTION civ.cards_due_tenants(integer, interval) TO ai_civ_app;
