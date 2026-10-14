-- Which step of a workflow each of the owner's turns to a coding agent works on,
-- read by the local model: the stage of the product (vision, design, website,
-- ...), the part of a website (colour, logo, ...), and whether the turn builds
-- the step, redoes it, or makes a small fix. Only these fixed labels are kept,
-- never the owner's words, and only a local model writes them.
--
-- A derived cache, not a record: rows are replaced when the turn's content or
-- the labeller changes, and go with the entry they label.
--
-- Nothing writes this table yet. The labeller was parked on 2026-09-25 before
-- release because it learned nothing measurable (reports/VARYZ_SHIP_2026-09-25.md);
-- the migration stays because archives already have version 40 applied, and a
-- different 0040 would be skipped on them.
CREATE TABLE workflow_turn_labels (
    entry_id      TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    content_hash  TEXT NOT NULL,   -- entries.content_hash when labelled
    labeller      TEXT NOT NULL,   -- model and prompt version
    step          TEXT NOT NULL,
    part          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('new', 'redo', 'fix', 'other')),
    labelled_at   TEXT NOT NULL
);
