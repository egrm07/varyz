-- Varyz canonical schema.
-- Entries are immutable ground truth; facets (todos, assertions, feelings,
-- life_events) are LLM-derived and re-derivable; summaries are cache, not truth.

CREATE TABLE entries (
    id              TEXT PRIMARY KEY,                 -- ULID
    kind            TEXT NOT NULL CHECK (kind IN
                      ('message','email','journal','calendar_event',
                       'photo','note','document','transcript')),
    source          TEXT NOT NULL,                    -- connector id: 'gmail', 'manual', ...
    source_account  TEXT NOT NULL DEFAULT '',         -- '' not NULL: NULLs never conflict in UNIQUE
    external_id     TEXT NOT NULL,                    -- upstream stable id (Message-ID, file hash, ULID)
    content_hash    TEXT NOT NULL,                    -- sha256 of normalized title+body
    title           TEXT,
    body            TEXT NOT NULL DEFAULT '',
    occurred_start  TEXT,                             -- ISO-8601 UTC
    occurred_end    TEXT,
    tz              TEXT,                             -- original timezone, informational
    time_precision  TEXT NOT NULL DEFAULT 'exact' CHECK (time_precision IN
                      ('exact','day','month','year','decade','unknown')),
    captured_at     TEXT,                             -- when the artifact was created upstream
    ingested_at     TEXT NOT NULL,
    vault_ref       TEXT,                             -- relative path into the raw-file vault
    thread_id       TEXT,                             -- upstream conversation/thread grouping
    sensitivity     TEXT NOT NULL DEFAULT 'unclassified' CHECK (sensitivity IN
                      ('public','personal','sensitive','unclassified')),
    enrich_version  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, source_account, external_id)
);

CREATE INDEX idx_entries_occurred ON entries (occurred_start);
CREATE INDEX idx_entries_kind ON entries (kind);
CREATE INDEX idx_entries_thread ON entries (thread_id);
CREATE INDEX idx_entries_sensitivity ON entries (sensitivity);

-- Entities ---------------------------------------------------------------

CREATE TABLE people (
    id                   TEXT PRIMARY KEY,
    canonical_name       TEXT NOT NULL,
    notes                TEXT,
    sensitivity_override TEXT CHECK (sensitivity_override IN
                           ('public','personal','sensitive'))
);

CREATE TABLE person_aliases (
    person_id   TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    alias_type  TEXT NOT NULL,          -- email | phone | wa_handle | tg_handle | display_name
    alias_value TEXT NOT NULL,
    UNIQUE (alias_type, alias_value)
);

CREATE TABLE places (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    lat     REAL,
    lon     REAL,
    geohash TEXT
);

CREATE TABLE topics (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'topic'  -- topic | project | chapter-tag
);

CREATE TABLE entry_people (
    entry_id  TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'mentioned',  -- author|recipient|mentioned|present
    UNIQUE (entry_id, person_id, role)
);

CREATE TABLE entry_places (
    entry_id   TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    place_id   TEXT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 1.0,
    UNIQUE (entry_id, place_id)
);

CREATE TABLE entry_topics (
    entry_id   TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    topic_id   TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 1.0,
    UNIQUE (entry_id, topic_id)
);

-- Facets: LLM-derived, re-derivable, deletable -----------------------------

CREATE TABLE todos (
    id           TEXT PRIMARY KEY,
    entry_id     TEXT REFERENCES entries(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                   ('open','done','dropped','superseded')),
    due          TEXT,
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    source_quote TEXT                    -- verbatim origin, for auditability
);
CREATE INDEX idx_todos_status ON todos (status);

CREATE TABLE assertions (
    id                TEXT PRIMARY KEY,
    entry_id          TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    subject_person_id TEXT REFERENCES people(id) ON DELETE SET NULL,
    predicate         TEXT NOT NULL,
    object_text       TEXT NOT NULL,
    as_of             TEXT
);

CREATE TABLE feelings (
    id        TEXT PRIMARY KEY,
    entry_id  TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    emotion   TEXT NOT NULL,
    valence   REAL,                      -- -1.0 .. 1.0
    intensity REAL                       --  0.0 .. 1.0
);

CREATE TABLE life_events (
    id       TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    weight   REAL NOT NULL DEFAULT 0.5
);

-- Hierarchical narrative (cache, regenerable) ------------------------------

CREATE TABLE summaries (
    id             TEXT PRIMARY KEY,
    period_type    TEXT NOT NULL CHECK (period_type IN
                     ('day','week','month','year','chapter')),
    period_key     TEXT NOT NULL,        -- '2026-07-03' | '2026-W27' | '2026-07' | '2026' | chapter id
    body_md        TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    generated_at   TEXT NOT NULL,
    dirty          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (period_type, period_key)
);

CREATE TABLE chapters (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT,
    thesis_md  TEXT
);

-- Search -------------------------------------------------------------------

CREATE VIRTUAL TABLE entries_fts USING fts5(
    title, body,
    content='entries', content_rowid='rowid'
);

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

CREATE TABLE chunks (
    id          INTEGER PRIMARY KEY,     -- rowid alias; vec_chunks rows share it
    entry_id    TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embed_model TEXT,                    -- NULL until embedded
    UNIQUE (entry_id, seq)
);
CREATE INDEX idx_chunks_entry ON chunks (entry_id);

CREATE VIRTUAL TABLE vec_chunks USING vec0(
    embedding float[384]
);

-- Pipeline bookkeeping -------------------------------------------------------

CREATE TABLE sync_state (
    connector_id TEXT NOT NULL,
    account      TEXT NOT NULL DEFAULT '',
    cursor_json  TEXT,
    last_run_at  TEXT,
    last_status  TEXT,
    PRIMARY KEY (connector_id, account)
);

CREATE TABLE enrichment_jobs (
    id         TEXT PRIMARY KEY,
    entry_id   TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    task       TEXT NOT NULL,            -- classify|extract_entities|extract_todos|embed|sensitivity
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                 ('pending','running','done','failed')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (entry_id, task)
);
CREATE INDEX idx_jobs_status ON enrichment_jobs (status);
