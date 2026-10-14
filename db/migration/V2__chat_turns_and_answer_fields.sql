ALTER TABLE chat_sessions
    ADD COLUMN preview           TEXT    NOT NULL DEFAULT '',
    ADD COLUMN title_set_by_user BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE chat_messages
    ADD COLUMN intent            VARCHAR(20),
    ADD COLUMN quote             JSONB,
    ADD COLUMN citations         JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN client_message_id UUID,
    ADD CONSTRAINT ck_chat_messages_intent
        CHECK (intent IS NULL OR intent IN ('ask', 'explain', 'related', 'flow'));

CREATE UNIQUE INDEX uq_chat_messages_session_client ON chat_messages (session_id, client_message_id)
    WHERE client_message_id IS NOT NULL;

CREATE TABLE chat_turns (
    id                 UUID        NOT NULL DEFAULT uuidv7(),
    session_id         UUID        NOT NULL,
    question_id        UUID,
    answer_id          UUID,
    status             VARCHAR(20) NOT NULL,
    failure_code       VARCHAR(64),
    model              VARCHAR(100),
    context_chars      INTEGER,
    input_tokens       INTEGER,
    cached_tokens      INTEGER,
    cache_write_tokens INTEGER,
    output_tokens      INTEGER,
    reasoning_tokens   INTEGER,
    snapshot_ms        INTEGER,
    first_token_ms     INTEGER,
    total_ms           INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    CONSTRAINT pk_chat_turns PRIMARY KEY (id),
    CONSTRAINT fk_chat_turns_session FOREIGN KEY (session_id)
        REFERENCES chat_sessions (id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_turns_question FOREIGN KEY (question_id)
        REFERENCES chat_messages (id) ON DELETE SET NULL,
    CONSTRAINT fk_chat_turns_answer FOREIGN KEY (answer_id)
        REFERENCES chat_messages (id) ON DELETE SET NULL,
    CONSTRAINT ck_chat_turns_status
        CHECK (status IN ('GENERATING', 'COMPLETE', 'CANCELLED', 'FAILED'))
);

CREATE UNIQUE INDEX uq_chat_turns_generating ON chat_turns (session_id)
    WHERE status = 'GENERATING';

CREATE INDEX ix_chat_turns_question ON chat_turns (question_id);
