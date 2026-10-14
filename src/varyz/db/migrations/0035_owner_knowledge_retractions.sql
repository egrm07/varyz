-- The owner can withdraw a belief without rewriting history.  A retraction is
-- its own append-only, device-stamped row; the retracted knowledge row stays
-- intact for audit and is excluded from resolution.
CREATE TABLE owner_knowledge_retractions (
    id                    TEXT PRIMARY KEY,
    knowledge_id          TEXT NOT NULL UNIQUE REFERENCES owner_knowledge(id),
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

CREATE TRIGGER owner_knowledge_retractions_no_update
BEFORE UPDATE ON owner_knowledge_retractions
BEGIN
    SELECT RAISE(ABORT, 'owner_knowledge_retractions is append-only');
END;

CREATE TRIGGER owner_knowledge_retractions_no_delete
BEFORE DELETE ON owner_knowledge_retractions
BEGIN
    SELECT RAISE(ABORT, 'owner_knowledge_retractions is append-only');
END;
