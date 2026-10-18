-- Make the conclusions findable.
--
-- A conclusion is the only thing a conversation produces that is allowed to
-- leave the machine, so it is the only thing worth searching for on the way to
-- answering a question. Searching the messages instead would find the right
-- conversation and then have nothing it could quote.
--
-- Porter stemming and unicode61, matching `entries_fts`: a question's wording
-- should not have to match the sentence a 12B model happened to write, and the
-- two indexes are fused by the same ranker, so tokenizing them differently
-- would put their scores on different scales.
--
-- External-content table over `conversation_window_conclusions`. The triggers
-- carry deletes through, which matters more here than for entries: a re-read
-- deletes and rewrites every conclusion for a window, and a stale index row
-- would go on returning a sentence whose conversation has since been recut.
CREATE VIRTUAL TABLE conversation_conclusions_fts USING fts5(
    text,
    content='conversation_window_conclusions', content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER conversation_conclusions_fts_ai
AFTER INSERT ON conversation_window_conclusions BEGIN
    INSERT INTO conversation_conclusions_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER conversation_conclusions_fts_ad
AFTER DELETE ON conversation_window_conclusions BEGIN
    INSERT INTO conversation_conclusions_fts(conversation_conclusions_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
END;

CREATE TRIGGER conversation_conclusions_fts_au
AFTER UPDATE OF text ON conversation_window_conclusions BEGIN
    INSERT INTO conversation_conclusions_fts(conversation_conclusions_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
    INSERT INTO conversation_conclusions_fts(rowid, text) VALUES (new.rowid, new.text);
END;
