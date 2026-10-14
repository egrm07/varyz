-- Durable owner questions.  Unlike owner_knowledge this is a queue and may
-- transition from pending to answered/expired in place; its answer is also
-- preserved as an immutable knowledge row.
CREATE TABLE resolution_escalations (
    id                    TEXT PRIMARY KEY,
    question              TEXT NOT NULL,
    normalized_question   TEXT NOT NULL,
    caller_agent_id       TEXT NOT NULL,
    caller_display_name   TEXT NOT NULL,
    caller_purpose        TEXT NOT NULL,
    caller_capability_id  TEXT,
    caller_device_id      TEXT NOT NULL,
    caller_transport      TEXT NOT NULL,
    purpose               TEXT NOT NULL,
    conversation_id       TEXT,
    tried_tiers_json      TEXT NOT NULL,
    urgency               TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'expired')),
    answer_knowledge_id   TEXT REFERENCES owner_knowledge(id),
    answer_resolution_json TEXT,
    created_device_id     TEXT NOT NULL,
    created_monotonic_seq INTEGER NOT NULL CHECK (created_monotonic_seq >= 1),
    answered_device_id    TEXT,
    answered_monotonic_seq INTEGER,
    created_at            TEXT NOT NULL,
    answered_at           TEXT,
    expires_at            TEXT,
    UNIQUE (created_device_id, created_monotonic_seq)
);

CREATE UNIQUE INDEX idx_resolution_escalations_pending_question
    ON resolution_escalations(normalized_question)
    WHERE status = 'pending';
CREATE INDEX idx_resolution_escalations_status_created
    ON resolution_escalations(status, created_at);

-- Every call leaves a self-contained audit row.  Serialized request/result
-- snapshots are the named future end-to-end encryption boundary.
CREATE TABLE resolution_ledger (
    id                    TEXT PRIMARY KEY,
    question              TEXT NOT NULL,
    normalized_question   TEXT NOT NULL,
    caller_agent_id       TEXT NOT NULL,
    caller_display_name   TEXT NOT NULL,
    caller_purpose        TEXT NOT NULL,
    caller_capability_id  TEXT,
    caller_device_id      TEXT NOT NULL,
    caller_transport      TEXT NOT NULL,
    purpose               TEXT NOT NULL,
    conversation_id       TEXT,
    disposition           TEXT NOT NULL,
    basis                 TEXT,
    answer                TEXT,
    tier                  INTEGER NOT NULL,
    confidence            REAL NOT NULL,
    escalation_id         TEXT,
    egress_request_id     TEXT,
    policy_version        TEXT NOT NULL,
    citations_json        TEXT NOT NULL,
    withheld_json         TEXT NOT NULL,
    request_json          TEXT NOT NULL,
    resolution_json       TEXT NOT NULL,
    device_id             TEXT NOT NULL,
    monotonic_seq         INTEGER NOT NULL CHECK (monotonic_seq >= 1),
    resolved_at           TEXT NOT NULL,
    UNIQUE (device_id, monotonic_seq)
);

CREATE INDEX idx_resolution_ledger_question
    ON resolution_ledger(normalized_question, monotonic_seq DESC);

CREATE TRIGGER resolution_ledger_no_update
BEFORE UPDATE ON resolution_ledger
BEGIN
    SELECT RAISE(ABORT, 'resolution_ledger is append-only');
END;

CREATE TRIGGER resolution_ledger_no_delete
BEFORE DELETE ON resolution_ledger
BEGIN
    SELECT RAISE(ABORT, 'resolution_ledger is append-only');
END;
