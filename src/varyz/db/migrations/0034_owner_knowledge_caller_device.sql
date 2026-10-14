-- Migration 0032 shipped before the caller's device stamp was added to the
-- frozen resolution contract. Keep 0032 immutable for archives that already
-- recorded it and add the missing provenance field forward-only.
ALTER TABLE owner_knowledge
    ADD COLUMN caller_device_id TEXT NOT NULL DEFAULT 'device:local';
