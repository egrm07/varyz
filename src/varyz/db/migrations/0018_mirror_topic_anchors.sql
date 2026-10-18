ALTER TABLE mirror_surface_topics
    ADD COLUMN anchor_key TEXT NOT NULL DEFAULT '';

-- Existing rows cannot be backfilled because they never stored an entry id.
-- Their empty anchor never matches a claim; recording them again supplies it.
CREATE INDEX IF NOT EXISTS idx_mirror_topics_anchor
    ON mirror_surface_topics (anchor_key);
