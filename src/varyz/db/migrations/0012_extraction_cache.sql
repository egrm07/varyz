-- Per-entry extraction cache: the graph stops re-reading what it has read.
--
-- A generation is keyed by a hash of every entry's content, so one new message
-- makes a new generation and the build restarts at entry zero. That is correct
-- as a *naming* rule — two generations built from different archives are
-- different readings — but it was also being used as the unit of *work*, so
-- ingesting a single WhatsApp message re-extracted 45,000 entries and re-ran
-- thousands of model calls. Six generations were started in the 48 hours
-- before this migration and one of them finished.
--
-- This table is keyed by content rather than by generation, because what an
-- extractor concluded about one entry is reusable by every later generation
-- that reads that entry the same way. `cache_key` folds in everything that can
-- change the answer: the extractor's name, version, model and prompt, the
-- privacy policy and mode the broker enforced, the batching limits that decide
-- what the model sees, the entry's own content and sensitivity label, and a
-- fingerprint of the conversation window a context-aware extractor is allowed
-- to read around it. A hit therefore means "this exact extractor has already
-- answered this exact question", and anything else that could change the
-- answer is a bug rather than a cache miss.
--
-- The rows hold extracted claims and their quotes, which is the same class of
-- personal content as `claims` and `claim_evidence`. Deleting an entry deletes
-- them with it.
CREATE TABLE graph_extraction_cache (
    cache_key          TEXT PRIMARY KEY,
    entry_id           TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    scope              TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    window_fingerprint TEXT NOT NULL,
    batch_json         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    last_used_at       TEXT NOT NULL
);

CREATE INDEX idx_graph_extraction_cache_entry ON graph_extraction_cache (entry_id);
CREATE INDEX idx_graph_extraction_cache_used ON graph_extraction_cache (last_used_at);
