-- How the owner guides an agent through a kind of work: the steps, in their order
-- ("website: colour, then logo, then what goes on the page"; "product: vision,
-- design, monetization"). Append-only and device-stamped like owner_principles.
--
-- basis 'stated' is the owner's own order, recorded from their words; 'learned'
-- is an order varyz found repeated across the owner's sessions with agents. A
-- learned order reaches agents at once (the owner chose that), marked as learned,
-- and never replaces a stated one. Changing an order appends a row that
-- supersedes the old one; withdrawing appends a retraction. Nothing is rewritten.
CREATE TABLE owner_workflows (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    normalized_name       TEXT NOT NULL,
    scope                 TEXT NOT NULL,          -- '*' for every project, else a project name
    domain                TEXT NOT NULL DEFAULT '', -- a kind of work ('website') or '' for free-standing
    steps_json            TEXT NOT NULL,          -- [{"step": "...", "how": "..."}], in order
    rationale             TEXT NOT NULL DEFAULT '',
    basis                 TEXT NOT NULL CHECK (basis IN ('stated', 'learned')),
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
    supersedes_id         TEXT REFERENCES owner_workflows(id),
    created_at            TEXT NOT NULL,
    UNIQUE (device_id, monotonic_seq)
);

CREATE INDEX idx_owner_workflows_name ON owner_workflows(normalized_name, scope);
CREATE INDEX idx_owner_workflows_supersedes ON owner_workflows(supersedes_id);

CREATE TRIGGER owner_workflows_no_update
BEFORE UPDATE ON owner_workflows
BEGIN
    SELECT RAISE(ABORT, 'owner_workflows is append-only');
END;

CREATE TRIGGER owner_workflows_no_delete
BEFORE DELETE ON owner_workflows
BEGIN
    SELECT RAISE(ABORT, 'owner_workflows is append-only');
END;

CREATE TABLE owner_workflow_retractions (
    id                    TEXT PRIMARY KEY,
    workflow_id           TEXT NOT NULL UNIQUE REFERENCES owner_workflows(id),
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

CREATE TRIGGER owner_workflow_retractions_no_update
BEFORE UPDATE ON owner_workflow_retractions
BEGIN
    SELECT RAISE(ABORT, 'owner_workflow_retractions is append-only');
END;

CREATE TRIGGER owner_workflow_retractions_no_delete
BEFORE DELETE ON owner_workflow_retractions
BEGIN
    SELECT RAISE(ABORT, 'owner_workflow_retractions is append-only');
END;
