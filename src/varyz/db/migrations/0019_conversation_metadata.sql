-- Source-native conversation structure is an additive projection of an entry.
-- Existing archives remain readable with an empty envelope; re-syncing a
-- connector backfills metadata even when the entry's content hash is unchanged.
ALTER TABLE entries
    ADD COLUMN conversation_metadata_json TEXT NOT NULL DEFAULT '{}';
