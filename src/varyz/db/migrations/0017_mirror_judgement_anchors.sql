ALTER TABLE mirror_surface_judgements
    ADD COLUMN anchor_key TEXT NOT NULL DEFAULT '';

-- Existing rows cannot be backfilled because they never stored an entry id.
-- Their empty anchor never matches a claim; recording them again supplies it.
CREATE INDEX IF NOT EXISTS idx_mirror_judgements_anchor
    ON mirror_surface_judgements (anchor_key);
