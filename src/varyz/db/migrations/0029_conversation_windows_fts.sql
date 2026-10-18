-- A conversation as a thing that can be searched for.
--
-- The archive's retrieval unit is an entry, and on a chat connector an entry
-- is one message with a median length of 47 characters. BM25 has almost
-- nothing to weigh in it and an embedding of it is closer to noise than to
-- meaning, which is why searching this archive for something that was plainly
-- discussed in it returns the wrong side of the exchange, or nothing.
--
-- This indexes the conversation instead: the rendered window, both sides, in
-- order. Unlike every other FTS table here it is not external-content, because
-- the text it indexes is assembled from many rows rather than stored in one.
-- It therefore holds its own copy, kept in step by the partition builder --
-- the same place that decides a window exists at all, so a window and its
-- index cannot disagree about what is in it.
--
-- What this table does NOT decide is who may read a window. That is the local
-- model's label in `conversation_window_labels`, applied at query time. A
-- window with no label has never been read and stays shut: unclassified is
-- sensitive here, as everywhere else.
CREATE VIRTUAL TABLE conversation_windows_fts USING fts5(
    window_id UNINDEXED,
    text,
    tokenize='porter unicode61'
);
