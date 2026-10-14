-- Stemmed full-text index.
--
-- The original index tokenized on exact word forms, so a search for "forked"
-- could not find a commit titled "Fork memoir into varyz" — the single most
-- likely first search a new user runs. Porter stemming collapses inflections
-- so a question's wording no longer has to match the document's.
--
-- FTS5 cannot alter a tokenizer in place; the table is dropped and rebuilt
-- from `entries`, which is the content source of truth. The triggers are
-- recreated because dropping the table leaves them referring to nothing.

DROP TRIGGER IF EXISTS entries_fts_ai;
DROP TRIGGER IF EXISTS entries_fts_ad;
DROP TRIGGER IF EXISTS entries_fts_au;
DROP TABLE IF EXISTS entries_fts;

CREATE VIRTUAL TABLE entries_fts USING fts5(
    title, body,
    content='entries', content_rowid='rowid',
    tokenize='porter unicode61'
);

INSERT INTO entries_fts(entries_fts) VALUES ('rebuild');

CREATE TRIGGER entries_fts_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
CREATE TRIGGER entries_fts_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
END;
CREATE TRIGGER entries_fts_au AFTER UPDATE OF title, body ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
    INSERT INTO entries_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
