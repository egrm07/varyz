-- Agent reports are not owner knowledge and never enter entries or the graph.
CREATE TABLE session_progress (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    project TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('working','blocked','complete','abandoned')),
    sensitivity TEXT NOT NULL CHECK(sensitivity IN ('personal','sensitive')),
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    UNIQUE(agent_id, project, conversation_id, checkpoint_id)
);
CREATE INDEX session_progress_scope ON session_progress(project, conversation_id, seq DESC);
CREATE VIRTUAL TABLE session_progress_fts USING fts5(task, facts);
-- FTS is maintained by the application in the same write transaction. This
-- table is not an archive source and is excluded from compile and Cloud sync.
