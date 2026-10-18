-- What the owner answered when an agent pointed out a workflow step they usually
-- settle first, and what they decided when varyz asked whether to keep a step they
-- keep skipping. Append-only and device-stamped like owner_workflows.
--
-- A nudge answer is 'took' (the owner settled the earlier step first), 'skipped'
-- (they went on with what they asked for) or 'rework' (a later task redid the
-- skipped-to step once the skipped step was settled; skip_id names that skip).
-- Skips with no rework after them are how varyz learns a step may not belong in
-- the order; it never drops one itself, it asks once (owner_workflow_reviews).
CREATE TABLE owner_workflow_nudges (
    id                    TEXT PRIMARY KEY,
    workflow_id           TEXT NOT NULL REFERENCES owner_workflows(id),
    step                  TEXT NOT NULL,          -- the earlier step the agent offered
    normalized_step       TEXT NOT NULL,
    task_step             TEXT NOT NULL DEFAULT '', -- the step the task started at
    project               TEXT NOT NULL DEFAULT '',
    answer                TEXT NOT NULL CHECK (answer IN ('took', 'skipped', 'rework')),
    skip_id               TEXT REFERENCES owner_workflow_nudges(id),
    caller_agent_id       TEXT NOT NULL,
    caller_display_name   TEXT NOT NULL,
    caller_purpose        TEXT NOT NULL,
    caller_capability_id  TEXT,
    caller_device_id      TEXT NOT NULL,
    caller_transport      TEXT NOT NULL,
    device_id             TEXT NOT NULL,
    monotonic_seq         INTEGER NOT NULL CHECK (monotonic_seq >= 1),
    recorded_at           TEXT NOT NULL,
    CHECK ((answer = 'rework') = (skip_id IS NOT NULL)),
    UNIQUE (device_id, monotonic_seq)
);

CREATE INDEX idx_owner_workflow_nudges_workflow
    ON owner_workflow_nudges(workflow_id, normalized_step, answer);
-- A skip leads to rework at most once.
CREATE UNIQUE INDEX idx_owner_workflow_nudges_rework
    ON owner_workflow_nudges(skip_id) WHERE skip_id IS NOT NULL;

CREATE TRIGGER owner_workflow_nudges_no_update
BEFORE UPDATE ON owner_workflow_nudges
BEGIN
    SELECT RAISE(ABORT, 'owner_workflow_nudges is append-only');
END;

CREATE TRIGGER owner_workflow_nudges_no_delete
BEFORE DELETE ON owner_workflow_nudges
BEGIN
    SELECT RAISE(ABORT, 'owner_workflow_nudges is append-only');
END;

-- The owner's answer when asked about a step they keep skipping: keep it, make it
-- optional, or drop it. 'optional' and 'drop' append a new version of the workflow
-- (result_workflow_id); 'keep' changes nothing. One answer per step per version.
CREATE TABLE owner_workflow_reviews (
    id                    TEXT PRIMARY KEY,
    workflow_id           TEXT NOT NULL REFERENCES owner_workflows(id),
    step                  TEXT NOT NULL,
    normalized_step       TEXT NOT NULL,
    decision              TEXT NOT NULL CHECK (decision IN ('keep', 'optional', 'drop')),
    result_workflow_id    TEXT REFERENCES owner_workflows(id),
    caller_agent_id       TEXT NOT NULL,
    caller_display_name   TEXT NOT NULL,
    caller_purpose        TEXT NOT NULL,
    caller_capability_id  TEXT,
    caller_device_id      TEXT NOT NULL,
    caller_transport      TEXT NOT NULL,
    device_id             TEXT NOT NULL,
    monotonic_seq         INTEGER NOT NULL CHECK (monotonic_seq >= 1),
    reviewed_at           TEXT NOT NULL,
    UNIQUE (workflow_id, normalized_step),
    UNIQUE (device_id, monotonic_seq)
);

CREATE TRIGGER owner_workflow_reviews_no_update
BEFORE UPDATE ON owner_workflow_reviews
BEGIN
    SELECT RAISE(ABORT, 'owner_workflow_reviews is append-only');
END;

CREATE TRIGGER owner_workflow_reviews_no_delete
BEFORE DELETE ON owner_workflow_reviews
BEGIN
    SELECT RAISE(ABORT, 'owner_workflow_reviews is append-only');
END;
