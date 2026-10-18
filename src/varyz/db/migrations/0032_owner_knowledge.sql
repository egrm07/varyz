-- Explicit owner knowledge is an append-only, device-stamped ledger.  A row is
-- intentionally self-contained so it can later be encrypted and synchronized
-- without shipping any other table.
CREATE TABLE owner_knowledge (
    id                    TEXT PRIMARY KEY,
    question              TEXT NOT NULL,
    normalized_question   TEXT NOT NULL,
    answer                TEXT NOT NULL,
    basis                 TEXT NOT NULL CHECK (basis IN ('explicit', 'user_confirmed')),
    source                TEXT NOT NULL,
    provenance_json       TEXT NOT NULL DEFAULT '{}',
    caller_agent_id       TEXT NOT NULL,
    caller_display_name   TEXT NOT NULL,
    caller_purpose        TEXT NOT NULL,
    caller_capability_id  TEXT,
    caller_transport      TEXT NOT NULL,
    device_id             TEXT NOT NULL,
    monotonic_seq         INTEGER NOT NULL CHECK (monotonic_seq >= 1),
    supersedes_id         TEXT REFERENCES owner_knowledge(id),
    escalation_id         TEXT,
    stated_at             TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (device_id, monotonic_seq)
);

CREATE INDEX idx_owner_knowledge_question
    ON owner_knowledge(normalized_question, monotonic_seq DESC);
CREATE INDEX idx_owner_knowledge_supersedes
    ON owner_knowledge(supersedes_id);

CREATE TRIGGER owner_knowledge_no_update
BEFORE UPDATE ON owner_knowledge
BEGIN
    SELECT RAISE(ABORT, 'owner_knowledge is append-only');
END;

CREATE TRIGGER owner_knowledge_no_delete
BEFORE DELETE ON owner_knowledge
BEGIN
    SELECT RAISE(ABORT, 'owner_knowledge is append-only');
END;

-- One sequence per device orders every resolution write even if wall time moves
-- backwards.  This small allocator is mutable; the ledgers it orders are not.
CREATE TABLE resolution_device_sequences (
    device_id       TEXT PRIMARY KEY,
    last_seq        INTEGER NOT NULL CHECK (last_seq >= 0)
);
