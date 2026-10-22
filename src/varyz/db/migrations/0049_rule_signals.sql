-- Whether rules held, and what the agent did before the owner corrected it.
--
-- rule_checks: one row each time a rule's result in a task changes: what
-- finish_check found (passed, failed, not_applicable, unchecked) or the owner
-- correcting the rule during that task (corrected). Append-only.
CREATE TABLE rule_checks (
    id                     TEXT PRIMARY KEY,
    principle_id           TEXT NOT NULL REFERENCES owner_principles(id),
    conversation_id        TEXT NOT NULL,
    project                TEXT,
    status                 TEXT NOT NULL CHECK (status IN
                            ('passed', 'failed', 'not_applicable', 'unchecked', 'corrected')),
    method                 TEXT NOT NULL,
    independently_verified INTEGER NOT NULL DEFAULT 0 CHECK (independently_verified IN (0, 1)),
    attempt                INTEGER NOT NULL CHECK (attempt >= 1),
    evidence               TEXT NOT NULL DEFAULT '',  -- redacted; never leaves this computer
    checked_at             TEXT NOT NULL
);
CREATE INDEX idx_rule_checks_task ON rule_checks(principle_id, conversation_id, checked_at);
CREATE TRIGGER rule_checks_no_update BEFORE UPDATE ON rule_checks
BEGIN SELECT RAISE(ABORT, 'rule_checks is append-only'); END;

-- correction_turns: the agent's own output the owner corrected, beside the rule
-- it taught: given by the agent, read from the chat's transcript, or from the
-- message the archive kept beside the owner's. Local only: chats never leave.
CREATE TABLE correction_turns (
    principle_id  TEXT PRIMARY KEY REFERENCES owner_principles(id),
    method        TEXT NOT NULL CHECK (method IN
                   ('agent_supplied', 'transcript', 'archive', 'not_found')),
    thread        TEXT NOT NULL DEFAULT '',
    agent_turn    TEXT NOT NULL DEFAULT '',
    owner_turn    TEXT NOT NULL DEFAULT '',
    captured_at   TEXT NOT NULL
);
