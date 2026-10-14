-- Historical reports keep their source time, not the time of a backfill.
ALTER TABLE session_progress ADD COLUMN origin TEXT NOT NULL DEFAULT 'agent_report'
    CHECK(origin IN ('agent_report','mined_report'));
ALTER TABLE session_progress ADD COLUMN occurred_at TEXT;
ALTER TABLE session_progress ADD COLUMN evidence_json TEXT;
UPDATE session_progress SET occurred_at=recorded_at;
CREATE INDEX session_progress_chronology ON session_progress(project, occurred_at DESC, seq DESC);
CREATE TABLE session_mining_files (
    scope TEXT NOT NULL,
    path_hash TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    PRIMARY KEY(scope, path_hash)
);
-- Erasure also prevents the next backfill from recreating erased reports.
CREATE TABLE session_mining_erased (
    project TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    PRIMARY KEY(project, conversation_id)
);
