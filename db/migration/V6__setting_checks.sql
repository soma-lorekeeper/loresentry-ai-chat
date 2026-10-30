CREATE TABLE setting_check_runs (
    id                UUID        NOT NULL DEFAULT uuidv7(),
    project_id        UUID        NOT NULL,
    user_id           UUID        NOT NULL,
    document_id       UUID        NOT NULL,
    document_title    TEXT        NOT NULL,
    revision_no       BIGINT      NOT NULL,
    status            VARCHAR(16) NOT NULL,
    stage             VARCHAR(16),
    read_manuscripts  INTEGER     NOT NULL DEFAULT 0,
    read_from_ordinal INTEGER,
    read_to_ordinal   INTEGER,
    read_settings     INTEGER     NOT NULL DEFAULT 0,
    read_core_facts   INTEGER     NOT NULL DEFAULT 0,
    failure_code      TEXT,
    model             TEXT,
    input_tokens      BIGINT      NOT NULL DEFAULT 0,
    cached_tokens     BIGINT      NOT NULL DEFAULT 0,
    output_tokens     BIGINT      NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    CONSTRAINT pk_setting_check_runs PRIMARY KEY (id),
    CONSTRAINT ck_setting_check_runs_status
        CHECK (status IN ('RUNNING', 'DONE', 'FAILED', 'CANCELLED')),
    CONSTRAINT ck_setting_check_runs_stage
        CHECK (stage IS NULL OR stage IN ('READING', 'FINDING', 'JUDGING'))
);

CREATE UNIQUE INDEX uq_setting_check_runs_running ON setting_check_runs (project_id)
    WHERE status = 'RUNNING';

CREATE INDEX ix_setting_check_runs_document
    ON setting_check_runs (project_id, document_id, created_at DESC);

CREATE INDEX ix_setting_check_runs_user ON setting_check_runs (user_id);

CREATE TABLE setting_check_findings (
    id                 UUID        NOT NULL DEFAULT uuidv7(),
    run_id             UUID        NOT NULL,
    position           INTEGER     NOT NULL,
    kind               VARCHAR(16) NOT NULL,
    core               BOOLEAN     NOT NULL DEFAULT false,
    confidence         VARCHAR(8)  NOT NULL,
    summary            TEXT        NOT NULL,
    direction          TEXT        NOT NULL,
    quote              TEXT        NOT NULL,
    source_document_id UUID        NOT NULL,
    source_title       TEXT        NOT NULL,
    source_doc_type    TEXT        NOT NULL,
    source_ordinal     INTEGER,
    source_quote       TEXT        NOT NULL,
    fingerprint        TEXT        NOT NULL,
    CONSTRAINT pk_setting_check_findings PRIMARY KEY (id),
    CONSTRAINT fk_setting_check_findings_run FOREIGN KEY (run_id)
        REFERENCES setting_check_runs (id) ON DELETE CASCADE,
    CONSTRAINT ck_setting_check_findings_kind CHECK (kind IN ('SETTING', 'MANUSCRIPT')),
    CONSTRAINT ck_setting_check_findings_confidence CHECK (confidence IN ('HIGH', 'LOW'))
);

CREATE INDEX ix_setting_check_findings_run ON setting_check_findings (run_id, position);

CREATE TABLE setting_check_verdicts (
    project_id         UUID        NOT NULL,
    user_id            UUID        NOT NULL,
    fingerprint        TEXT        NOT NULL,
    verdict            VARCHAR(16) NOT NULL,
    reason             TEXT,
    source_document_id UUID        NOT NULL,
    source_quote       TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_setting_check_verdicts PRIMARY KEY (project_id, fingerprint),
    CONSTRAINT ck_setting_check_verdicts_verdict CHECK (verdict IN ('INTENDED', 'NOT_CONFLICT')),
    CONSTRAINT ck_setting_check_verdicts_reason
        CHECK (reason IS NULL OR char_length(reason) <= 500)
);

CREATE INDEX ix_setting_check_verdicts_source
    ON setting_check_verdicts (project_id, source_document_id);

CREATE INDEX ix_setting_check_verdicts_user ON setting_check_verdicts (user_id);
