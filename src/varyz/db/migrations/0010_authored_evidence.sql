-- Who wrote this entry: the user, or somebody (or something) else.
--
-- The Mirror is an interpretation of the user, and the strongest way to get it
-- wrong is to read words *at* the user as words *from* the user. An agent's
-- reply, a friend's message, and a calendar invitation somebody else sent are
-- all evidence about the user's life; none of them is evidence of the user's
-- own beliefs, values, or goals. Until now nothing in the archive recorded the
-- difference, so a values section built from an AI-conversation import would
-- have been built mostly from the assistant's prose.
--
-- Additive and defaulted to 0: not-authored is the safe reading, and an entry
-- ingested before this column existed keeps it until its connector re-offers
-- the item. Only connectors that can *know* authorship set it.

ALTER TABLE entries ADD COLUMN authored_by_user INTEGER NOT NULL DEFAULT 0;

-- The coverage gate counts authored entries and measures their time span per
-- source, which is exactly this index's shape.
CREATE INDEX idx_entries_authored
    ON entries (source, authored_by_user, occurred_start);
