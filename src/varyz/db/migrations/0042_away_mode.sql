CREATE TABLE away_modes (
    scope TEXT NOT NULL CHECK (scope IN ('task', 'project', 'all')),
    project TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    instruction TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (scope, project, conversation_id)
);

-- A provisional agent choice is never owner_knowledge. The same unresolved
-- question can be relevant to several tasks without duplicating the question.
CREATE TABLE away_reviews (
    escalation_id TEXT NOT NULL REFERENCES resolution_escalations(id),
    project TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'preference'
        CHECK (kind IN ('preference', 'blocking', 'permission')),
    proposed_choice TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (escalation_id, project, conversation_id)
);
