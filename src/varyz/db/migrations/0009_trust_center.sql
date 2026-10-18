-- MVP trust-center controls.  Exclusions are local tombstones: they prevent a
-- connector from restoring an entry that the user explicitly removed from
-- Varyz, without mutating the upstream file, message, event, or account.

CREATE TABLE entry_exclusions (
    source          TEXT NOT NULL,
    source_account  TEXT NOT NULL DEFAULT '',
    external_id     TEXT NOT NULL,
    content_hash    TEXT,
    reason          TEXT NOT NULL DEFAULT 'user_forget',
    created_at      TEXT NOT NULL,
    PRIMARY KEY (source, source_account, external_id)
);
CREATE INDEX idx_entry_exclusions_created
    ON entry_exclusions (created_at);
