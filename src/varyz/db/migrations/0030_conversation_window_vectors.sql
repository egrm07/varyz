-- Meaning, for a conversation, alongside the words it used.
--
-- 0029 made a conversation findable by its words. Measured on the real
-- archive, BM25 over a whole window puts the right conversation first 45.8% of
-- the time -- better than searching 47-character messages, and still a coin
-- flip. The misses are the ordinary ones: the question asks about the plumber
-- and the conversation says "he", asks about rent and the conversation says
-- "the money for the flat".
--
-- So the window text is embedded too, in chunks, exactly as an entry's text
-- is. Separate tables rather than a column on `chunks`, for two reasons: the
-- entry chunk table is what every existing retrieval path reads, and a window
-- chunk arriving in those results would be a message quoted where a passage
-- was expected; and this whole layer is meant to stay droppable -- two tables
-- go away without touching the index the rest of varyz depends on.
--
-- Like the FTS table, this decides nothing about who may read a window. The
-- label does, at query time.
CREATE TABLE conversation_window_chunks (
    id          INTEGER PRIMARY KEY,     -- rowid alias; vec rows share it
    window_id   TEXT NOT NULL REFERENCES conversation_windows(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    content_hash TEXT NOT NULL,          -- the window's hash when this was cut
    embed_model TEXT,                    -- NULL until embedded
    UNIQUE (window_id, seq)
);
CREATE INDEX idx_window_chunks_window ON conversation_window_chunks (window_id);
CREATE INDEX idx_window_chunks_pending ON conversation_window_chunks (embed_model);

CREATE VIRTUAL TABLE vec_window_chunks USING vec0(
    embedding float[384]
);
