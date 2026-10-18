-- Record how each chunk was cut, so changing the cut re-cuts.
--
-- `embed_pending` chunks entries that have no chunks, which means an entry is
-- chunked exactly once, by whatever the rules were that day, forever. Editing
-- `embedding.chunk_tokens` therefore did nothing to an existing archive, and
-- neither did fixing the splitter — the setting and the fix both appeared to
-- apply and silently didn't, which is the same shape of bug as the `dims`
-- setting that was believed until the first insert failed.
--
-- The recipe string names the splitter version and its budget. Existing rows
-- get NULL, which differs from every real recipe, so the next `varyz embed`
-- re-chunks and re-embeds the archive once and then settles.

ALTER TABLE chunks ADD COLUMN chunk_recipe TEXT;
