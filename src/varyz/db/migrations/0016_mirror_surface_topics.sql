CREATE TABLE IF NOT EXISTS mirror_surface_topics (
    surface_key    TEXT PRIMARY KEY,
    surface        TEXT NOT NULL,
    topic          TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mirror_topics_topic
    ON mirror_surface_topics (prompt_version, topic);
