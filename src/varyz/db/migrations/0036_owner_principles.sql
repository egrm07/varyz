-- How the owner builds things: standing rules and project vision that agents are
-- advised with before they plan or implement. Append-only and device-stamped like
-- owner_knowledge. A mined suggestion is a 'candidate' until the owner confirms it;
-- confirming appends a 'confirmed' row that supersedes the candidate, and rejecting
-- or withdrawing appends a retraction. Nothing is rewritten.
CREATE TABLE owner_principles (
    id                    TEXT PRIMARY KEY,
    statement             TEXT NOT NULL,
    normalized_statement  TEXT NOT NULL,
    kind                  TEXT NOT NULL CHECK (kind IN ('rule', 'vision')),
    scope                 TEXT NOT NULL,          -- '*' for every project, else a project name
    rationale             TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL CHECK (status IN ('candidate', 'confirmed')),
    source                TEXT NOT NULL,
    provenance_json       TEXT NOT NULL DEFAULT '{}',
    caller_agent_id       TEXT NOT NULL,
    caller_display_name   TEXT NOT NULL,
    caller_purpose        TEXT NOT NULL,
    caller_capability_id  TEXT,
    caller_device_id      TEXT NOT NULL,
    caller_transport      TEXT NOT NULL,
    device_id             TEXT NOT NULL,
    monotonic_seq         INTEGER NOT NULL CHECK (monotonic_seq >= 1),
    supersedes_id         TEXT REFERENCES owner_principles(id),
    created_at            TEXT NOT NULL,
    UNIQUE (device_id, monotonic_seq)
);

CREATE INDEX idx_owner_principles_statement
    ON owner_principles(normalized_statement, scope);
CREATE INDEX idx_owner_principles_supersedes
    ON owner_principles(supersedes_id);

CREATE TRIGGER owner_principles_no_update
BEFORE UPDATE ON owner_principles
BEGIN
    SELECT RAISE(ABORT, 'owner_principles is append-only');
END;

CREATE TRIGGER owner_principles_no_delete
BEFORE DELETE ON owner_principles
BEGIN
    SELECT RAISE(ABORT, 'owner_principles is append-only');
END;

CREATE TABLE owner_principle_retractions (
    id                    TEXT PRIMARY KEY,
    principle_id          TEXT NOT NULL UNIQUE REFERENCES owner_principles(id),
    reason                TEXT NOT NULL DEFAULT '',
    caller_agent_id       TEXT NOT NULL,
    caller_display_name   TEXT NOT NULL,
    caller_purpose        TEXT NOT NULL,
    caller_capability_id  TEXT,
    caller_device_id      TEXT NOT NULL,
    caller_transport      TEXT NOT NULL,
    device_id             TEXT NOT NULL,
    monotonic_seq         INTEGER NOT NULL CHECK (monotonic_seq >= 1),
    retracted_at          TEXT NOT NULL,
    UNIQUE (device_id, monotonic_seq)
);

CREATE TRIGGER owner_principle_retractions_no_update
BEFORE UPDATE ON owner_principle_retractions
BEGIN
    SELECT RAISE(ABORT, 'owner_principle_retractions is append-only');
END;

CREATE TRIGGER owner_principle_retractions_no_delete
BEFORE DELETE ON owner_principle_retractions
BEGIN
    SELECT RAISE(ABORT, 'owner_principle_retractions is append-only');
END;
