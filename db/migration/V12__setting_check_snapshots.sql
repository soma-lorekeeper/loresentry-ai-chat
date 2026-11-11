ALTER TABLE setting_check_runs ADD COLUMN version_id UUID;

ALTER TABLE setting_check_findings
    ADD COLUMN verdict             VARCHAR(16),
    ADD COLUMN reason              TEXT,
    ADD COLUMN decided_at          TIMESTAMPTZ,
    ADD COLUMN decided_revision_no BIGINT,
    ADD CONSTRAINT ck_setting_check_findings_verdict
        CHECK (verdict IS NULL OR verdict IN ('INTENDED', 'NOT_CONFLICT')),
    ADD CONSTRAINT ck_setting_check_findings_reason
        CHECK (reason IS NULL OR char_length(reason) <= 500),
    ADD CONSTRAINT ck_setting_check_findings_decided
        CHECK ((verdict IS NULL) = (decided_at IS NULL)
           AND (verdict IS NULL) = (decided_revision_no IS NULL));

UPDATE setting_check_findings f
SET verdict = v.verdict,
    reason = v.reason,
    decided_at = v.updated_at,
    decided_revision_no = r.revision_no
FROM setting_check_runs r, setting_check_verdicts v
WHERE r.id = f.run_id
  AND v.project_id = r.project_id
  AND v.fingerprint = f.fingerprint;

DROP TABLE setting_check_verdicts;
