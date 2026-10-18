-- Seams for answers that are not only the local owner's, added before the first
-- public install so that sharing, published taste, adoption metrics and routed
-- questions are later additions rather than migrations of the owner's ledgers.

-- Who answered each knowledge entry, and where it came from.  'own' is this
-- install's owner (or someone they routed a question to); 'org' is an answer
-- shared across an organisation and scoped 'org:<id>'; 'published' is a creator's
-- published taste scoped 'published:<id>'.  Every existing row is the local
-- owner's own answer.  Resolution precedence is own project, own general ('*'),
-- org, published: the owner's own answer always wins.
ALTER TABLE owner_knowledge
    ADD COLUMN author_id TEXT NOT NULL DEFAULT 'owner:local';
ALTER TABLE owner_knowledge
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'own'
        CHECK (origin IN ('own', 'org', 'published'));

CREATE INDEX idx_owner_knowledge_origin ON owner_knowledge(origin, scope);

-- What became of each answer an agent was given.  Append-only and device-stamped
-- like the other ledgers: a later signal is a new row, never an edit.
-- knowledge_id names the owner-knowledge entry that caused the outcome, if any
-- (for 'overridden', the entry that superseded the answer).
CREATE TABLE resolution_outcomes (
    id                    TEXT PRIMARY KEY,
    ledger_id             TEXT NOT NULL REFERENCES resolution_ledger(id),
    outcome               TEXT NOT NULL
        CHECK (outcome IN ('adopted', 'overridden', 'corrected')),
    source                TEXT NOT NULL,
    knowledge_id          TEXT REFERENCES owner_knowledge(id),
    device_id             TEXT NOT NULL,
    monotonic_seq         INTEGER NOT NULL CHECK (monotonic_seq >= 1),
    recorded_at           TEXT NOT NULL,
    UNIQUE (device_id, monotonic_seq)
);

CREATE INDEX idx_resolution_outcomes_ledger
    ON resolution_outcomes(ledger_id, monotonic_seq);
CREATE INDEX idx_resolution_outcomes_outcome
    ON resolution_outcomes(outcome, recorded_at);

CREATE TRIGGER resolution_outcomes_no_update
BEFORE UPDATE ON resolution_outcomes
BEGIN
    SELECT RAISE(ABORT, 'resolution_outcomes is append-only');
END;

CREATE TRIGGER resolution_outcomes_no_delete
BEFORE DELETE ON resolution_outcomes
BEGIN
    SELECT RAISE(ABORT, 'resolution_outcomes is append-only');
END;

-- Who an escalated question is for.  Every question so far went to the owner.
ALTER TABLE resolution_escalations
    ADD COLUMN recipient_id TEXT NOT NULL DEFAULT 'owner:local';

-- One pending question per project and recipient: the same question routed to two
-- people is two questions, and one person's answer must not close the other's.
DROP INDEX idx_resolution_escalations_pending_question;
CREATE UNIQUE INDEX idx_resolution_escalations_pending_question
    ON resolution_escalations(normalized_question, COALESCE(project, ''), recipient_id)
    WHERE status = 'pending';
