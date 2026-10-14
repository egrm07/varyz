-- Significance: how much an entry matters to the life story (0.0 .. 1.0).
-- Scored locally by heuristic at ingest; optionally refined by the LLM
-- enrichment pass. Drives the timeline's default "important" view only —
-- search and ask always see everything.

ALTER TABLE entries ADD COLUMN significance REAL;
CREATE INDEX idx_entries_significance ON entries (significance);
