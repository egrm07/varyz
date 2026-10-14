-- The owner's way of working versus one-off product choices.
--
-- owner_knowledge.scope: '*' is a general preference that applies in every
-- project ("always self-host fonts"); a project name is a one-time choice for that
-- product ("persona's accent colour is #6d5efc") and never answers for another.
-- Existing rows keep '*', which is how tier one already treated them.
ALTER TABLE owner_knowledge ADD COLUMN scope TEXT NOT NULL DEFAULT '*';

-- owner_principles.domain: '' applies to any task; a domain such as 'website' makes
-- the rule part of that kind of work's definition of done, handed to an agent only
-- when the task is that kind of work.
ALTER TABLE owner_principles ADD COLUMN domain TEXT NOT NULL DEFAULT '';

-- The project an escalated question came from, so its answer can default to
-- the right scope when the owner replies.
ALTER TABLE resolution_escalations ADD COLUMN project TEXT;

CREATE INDEX idx_owner_knowledge_scope ON owner_knowledge(scope);
CREATE INDEX idx_owner_principles_domain ON owner_principles(domain, scope);

-- One pending question per project: "which accent colour?" asked from two
-- projects is two questions, and one answer must not silently close both.
DROP INDEX idx_resolution_escalations_pending_question;
CREATE UNIQUE INDEX idx_resolution_escalations_pending_question
    ON resolution_escalations(normalized_question, COALESCE(project, ''))
    WHERE status = 'pending';
