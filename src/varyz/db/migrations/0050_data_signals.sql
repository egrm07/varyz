-- Which agent and model did the work a rule was checked in; whether memory was
-- shown; whether a question needed asking; and how well the owner's answer was
-- predicted before it came in. All local; see docs/NETWORK_DATA.md for what an
-- opted-in owner shares.

-- The client and model that ran finish_check, as the agent reported them.
ALTER TABLE rule_checks ADD COLUMN agent TEXT NOT NULL DEFAULT '';
ALTER TABLE rule_checks ADD COLUMN model TEXT NOT NULL DEFAULT '';

-- rule_exposures: for an owner who turned the memory test on, whether a rule was
-- shown at the start of a task or held back until finish_check. The arm is drawn
-- from a hash of the rule and the task, so every briefing in a task agrees.
CREATE TABLE rule_exposures (
    principle_id    TEXT NOT NULL REFERENCES owner_principles(id),
    conversation_id TEXT NOT NULL,
    arm             TEXT NOT NULL CHECK (arm IN ('shown', 'held_out')),
    rate            REAL NOT NULL CHECK (rate > 0 AND rate < 1),
    recorded_at     TEXT NOT NULL,
    PRIMARY KEY (principle_id, conversation_id)
);

-- ask_feedback: the owner saying a question was not needed, or that the agent
-- should have asked before acting, as the agent relayed it.
CREATE TABLE ask_feedback (
    id              TEXT PRIMARY KEY,
    escalation_id   TEXT REFERENCES resolution_escalations(id),
    principle_id    TEXT REFERENCES owner_principles(id),
    conversation_id TEXT,
    label           TEXT NOT NULL CHECK (label IN ('needed', 'unneeded', 'should_have_asked')),
    recorded_at     TEXT NOT NULL,
    CHECK (escalation_id IS NOT NULL OR principle_id IS NOT NULL)
);
CREATE UNIQUE INDEX idx_ask_feedback_escalation ON ask_feedback(escalation_id)
    WHERE escalation_id IS NOT NULL;

-- answer_predictions: before a question reached the owner, the asking agent's
-- best guess at the answer and the nearest answer the owner gave before. Scored
-- against the owner's real answer when it arrives.
CREATE TABLE answer_predictions (
    escalation_id   TEXT PRIMARY KEY REFERENCES resolution_escalations(id),
    decision_kind   TEXT NOT NULL DEFAULT '',
    agent           TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    agent_guess     TEXT NOT NULL DEFAULT '',
    memory_guess    TEXT NOT NULL DEFAULT '',
    memory_source   TEXT,
    predicted_at    TEXT NOT NULL
);
CREATE TRIGGER answer_predictions_no_update BEFORE UPDATE ON answer_predictions
BEGIN SELECT RAISE(ABORT, 'answer_predictions is append-only'); END;
