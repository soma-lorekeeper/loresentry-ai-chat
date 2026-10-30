ALTER TABLE setting_check_findings RENAME COLUMN direction TO explanation;
ALTER TABLE setting_check_findings DROP COLUMN summary;
