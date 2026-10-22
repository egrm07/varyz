-- How a task the agent had to work out gets done next time: a few steps, how to
-- check it worked, and the trap that cost time. Written by the agent at the end of
-- a task it figured out and verified, served with the next task of the same name.
-- Append-only and device-stamped like owner_workflows: a better recipe appends a
-- row that supersedes the old one, withdrawing appends a retraction, and each use
-- appends an outcome ('worked', 'changed', 'failed') so a recipe that stopped
-- working stops being served.
CREATE TABLE task_recipes (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    normalized_name       TEXT NOT NULL,
    scope                 TEXT NOT NULL,          -- '*' for every project, else a project name
    steps_json            TEXT NOT NULL,          -- [{"step": "...", "how": "..."}], in order
    check_text            TEXT NOT NULL,          -- how to tell it worked
    pitfalls              TEXT NOT NULL DEFAULT '',
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
    supersedes_id         TEXT REFERENCES task_recipes(id),
    created_at            TEXT NOT NULL,
    UNIQUE (device_id, monotonic_seq)
);

CREATE INDEX idx_task_recipes_name ON task_recipes(normalized_name, scope);
CREATE INDEX idx_task_recipes_supersedes ON task_recipes(supersedes_id);

CREATE TRIGGER task_recipes_no_update
BEFORE UPDATE ON task_recipes
BEGIN
    SELECT RAISE(ABORT, 'task_recipes is append-only');
END;

CREATE TRIGGER task_recipes_no_delete
BEFORE DELETE ON task_recipes
BEGIN
    SELECT RAISE(ABORT, 'task_recipes is append-only');
END;

CREATE TABLE task_recipe_retractions (
    id                    TEXT PRIMARY KEY,
    recipe_id             TEXT NOT NULL UNIQUE REFERENCES task_recipes(id),
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

CREATE TRIGGER task_recipe_retractions_no_update
BEFORE UPDATE ON task_recipe_retractions
BEGIN
    SELECT RAISE(ABORT, 'task_recipe_retractions is append-only');
END;

CREATE TRIGGER task_recipe_retractions_no_delete
BEFORE DELETE ON task_recipe_retractions
BEGIN
    SELECT RAISE(ABORT, 'task_recipe_retractions is append-only');
END;

CREATE TABLE task_recipe_outcomes (
    id                    TEXT PRIMARY KEY,
    recipe_id             TEXT NOT NULL REFERENCES task_recipes(id),
    outcome               TEXT NOT NULL CHECK (outcome IN ('worked', 'changed', 'failed')),
    note                  TEXT NOT NULL DEFAULT '',
    conversation_id       TEXT,
    caller_agent_id       TEXT NOT NULL,
    caller_display_name   TEXT NOT NULL,
    caller_purpose        TEXT NOT NULL,
    caller_capability_id  TEXT,
    caller_device_id      TEXT NOT NULL,
    caller_transport      TEXT NOT NULL,
    device_id             TEXT NOT NULL,
    monotonic_seq         INTEGER NOT NULL CHECK (monotonic_seq >= 1),
    recorded_at           TEXT NOT NULL,
    UNIQUE (device_id, monotonic_seq)
);

CREATE INDEX idx_task_recipe_outcomes_recipe ON task_recipe_outcomes(recipe_id, recorded_at);

CREATE TRIGGER task_recipe_outcomes_no_update
BEFORE UPDATE ON task_recipe_outcomes
BEGIN
    SELECT RAISE(ABORT, 'task_recipe_outcomes is append-only');
END;

CREATE TRIGGER task_recipe_outcomes_no_delete
BEFORE DELETE ON task_recipe_outcomes
BEGIN
    SELECT RAISE(ABORT, 'task_recipe_outcomes is append-only');
END;
