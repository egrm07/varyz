-- Report privacy checks use source + external_id without a source_account.
-- The existing unique index has source_account between these columns, so a
-- checkpoint backfill otherwise scans the conversation archive on every check.
CREATE INDEX idx_entries_report_privacy ON entries(source, external_id, sensitivity)
WHERE source='ai_conversations' AND sensitivity IN ('sensitive','unclassified');
