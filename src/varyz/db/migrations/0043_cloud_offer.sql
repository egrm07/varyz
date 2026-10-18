-- Device-local commercial notice controls, separate from owner knowledge/sync.
CREATE TABLE cloud_offer_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    offer_id TEXT,
    issued_at REAL,
    snoozed_until REAL NOT NULL DEFAULT 0,
    interested INTEGER NOT NULL DEFAULT 0 CHECK (interested IN (0, 1))
);
INSERT INTO cloud_offer_state(id) VALUES (1);
