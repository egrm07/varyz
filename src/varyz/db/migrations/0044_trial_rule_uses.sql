-- Which tasks a trial rule was handed to. A mined rule with strong evidence goes
-- into effect on trial (a confirmed owner_principles row whose provenance says
-- "trial") before the owner has seen it; once it was in the briefing of enough
-- different tasks over enough days with no correction, it is confirmed for good
-- (varyz.resolution.trials). One row per rule and task: the same conversation
-- briefed twice is one use.
CREATE TABLE owner_principle_uses (
    principle_id  TEXT NOT NULL REFERENCES owner_principles(id),
    task_key      TEXT NOT NULL,          -- the conversation id, or a hash of the task
    shown_at      TEXT NOT NULL,
    PRIMARY KEY (principle_id, task_key)
);
