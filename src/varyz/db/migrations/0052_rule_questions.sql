-- A waiting rule asked inside a task the owner was doing: one row when a briefing
-- asked about it, one when the owner answered ('yes' kept it, 'no' stopped it,
-- 'reword' kept it in their words). At most one rule is asked per conversation
-- (varyz.resolution.rule_questions). Append-only; local only.
CREATE TABLE rule_questions (
    principle_id     TEXT NOT NULL REFERENCES owner_principles(id),
    conversation_id  TEXT NOT NULL,
    project          TEXT,
    event            TEXT NOT NULL CHECK (event IN ('asked', 'yes', 'no', 'reword')),
    recorded_at      TEXT NOT NULL,
    PRIMARY KEY (principle_id, conversation_id, event)
);
CREATE INDEX idx_rule_questions_conversation ON rule_questions(conversation_id, event);
CREATE INDEX idx_rule_questions_recorded ON rule_questions(recorded_at);
CREATE TRIGGER rule_questions_no_update BEFORE UPDATE ON rule_questions
BEGIN SELECT RAISE(ABORT, 'rule_questions is append-only'); END;
CREATE TRIGGER rule_questions_no_delete BEFORE DELETE ON rule_questions
BEGIN SELECT RAISE(ABORT, 'rule_questions is append-only'); END;
