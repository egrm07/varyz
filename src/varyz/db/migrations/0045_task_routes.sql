-- Operational routing only. No task text, owner answers, grants or sync selection.
CREATE TABLE varyz_task_routes (
    namespace TEXT NOT NULL,
    project TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(namespace, project, conversation_id)
);
