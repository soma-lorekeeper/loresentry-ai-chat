CREATE TABLE chat_held_documents (
    session_id    UUID        NOT NULL,
    target_id     UUID        NOT NULL,
    kind          VARCHAR(10) NOT NULL,
    first_read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_chat_held_documents PRIMARY KEY (session_id, target_id),
    CONSTRAINT fk_chat_held_documents_session FOREIGN KEY (session_id)
        REFERENCES chat_sessions (id) ON DELETE CASCADE,
    CONSTRAINT ck_chat_held_documents_kind CHECK (kind IN ('DOCUMENT', 'MEMO'))
);

CREATE TABLE document_summaries (
    document_id    UUID        NOT NULL,
    project_id     UUID        NOT NULL,
    revision_no    BIGINT      NOT NULL,
    summary        TEXT        NOT NULL,
    fingerprint    JSONB       NOT NULL,
    model          VARCHAR(100),
    prompt_version VARCHAR(40) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_document_summaries PRIMARY KEY (document_id)
);

CREATE INDEX ix_document_summaries_project ON document_summaries (project_id);

CREATE TABLE summary_queue (
    document_id     UUID        NOT NULL,
    project_id      UUID        NOT NULL,
    revision_no     BIGINT      NOT NULL,
    folder_code     VARCHAR(40) NOT NULL,
    title           TEXT        NOT NULL,
    body            TEXT        NOT NULL,
    changed_at      TIMESTAMPTZ NOT NULL,
    attempts        INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_summary_queue PRIMARY KEY (document_id)
);

CREATE INDEX ix_summary_queue_due ON summary_queue (changed_at);
CREATE INDEX ix_summary_queue_project ON summary_queue (project_id);

ALTER TABLE chat_messages ADD COLUMN grounding JSONB;

ALTER TABLE chat_turns
    ADD COLUMN steps       INTEGER,
    ADD COLUMN judge_calls INTEGER,
    ADD COLUMN chars_read  INTEGER,
    ADD COLUMN grounding   VARCHAR(16),
    ADD COLUMN rejections  INTEGER,
    ADD COLUMN reviews     JSONB;
