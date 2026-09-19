CREATE TABLE chat_sessions (
    id         UUID         NOT NULL DEFAULT uuidv7(),
    project_id UUID         NOT NULL,
    user_id    UUID         NOT NULL,
    title      VARCHAR(200) NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    CONSTRAINT pk_chat_sessions PRIMARY KEY (id)
);

CREATE INDEX ix_chat_sessions_user_project_updated ON chat_sessions (user_id, project_id, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE chat_messages (
    id         UUID        NOT NULL DEFAULT uuidv7(),
    session_id UUID        NOT NULL,
    role       VARCHAR(20) NOT NULL,
    content_md TEXT        NOT NULL,
    status     VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_chat_messages PRIMARY KEY (id),
    CONSTRAINT fk_chat_messages_session FOREIGN KEY (session_id)
        REFERENCES chat_sessions (id) ON DELETE CASCADE,
    CONSTRAINT ck_chat_messages_role CHECK (role IN ('USER', 'ASSISTANT')),
    CONSTRAINT ck_chat_messages_status CHECK (status IN ('COMPLETE', 'FAILED', 'CANCELLED'))
);

CREATE INDEX ix_chat_messages_session_created ON chat_messages (session_id, created_at);
