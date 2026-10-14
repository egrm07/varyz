-- Varyz Mirror: the durable record of what Varyz currently concludes about the
-- user, and of every place the user overruled it.
--
-- Three things forced separate tables rather than one JSON blob per run.
--
-- A section is judged on its own evidence. Values can be Ready while
-- significant people are Unavailable, so the state, the numbers behind it, and
-- the missing coverage belong to the section, not to the Mirror. A single
-- overall confidence would let a well-covered section lend credibility to a
-- thin one, which is exactly the failure the product contract forbids.
--
-- Every conclusion has to stay inspectable. Evidence is rows referencing real
-- entries, not prose the generator wrote about its own reasoning, so
-- `why` can show the quote, the source, and the date, and counterevidence is
-- stored beside supporting evidence rather than being dropped for tidiness.
--
-- And a user edit must outlive the run it was made against. Regeneration
-- writes a whole new set of conclusions; if edits lived on the conclusion row
-- they would vanish, and deleted text would silently return as a fresh Varyz
-- conclusion. `mirror_edits` is therefore keyed by a content fingerprint that
-- survives regeneration, and generation applies it before anything is shown.

CREATE TABLE mirror_runs (
    id                   TEXT PRIMARY KEY,
    -- Which graph generation this reads. A Mirror is only as current as the
    -- graph under it, and pinning the generation makes that checkable.
    graph_generation_id  TEXT NOT NULL,
    generated_at         TEXT NOT NULL,
    state                TEXT NOT NULL DEFAULT 'current',   -- current | superseded
    gate_passed          INTEGER NOT NULL,
    gate_reason          TEXT NOT NULL,
    -- Per-source coverage and the thresholds in force, recorded as data so a
    -- later evaluation can say which numbers produced which Mirror.
    coverage_json        TEXT NOT NULL DEFAULT '{}',
    thresholds_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_mirror_runs_state ON mirror_runs (state, generated_at);

CREATE TABLE mirror_sections (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES mirror_runs(id) ON DELETE CASCADE,
    section           TEXT NOT NULL,     -- values | people | goals | questions
    state             TEXT NOT NULL,     -- ready | limited | unavailable
    confidence        REAL NOT NULL DEFAULT 0,
    measured_json     TEXT NOT NULL DEFAULT '{}',
    missing_coverage  TEXT NOT NULL DEFAULT '',
    unlock_hint       TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id, section)
);

CREATE TABLE mirror_conclusions (
    id                     TEXT PRIMARY KEY,
    run_id                 TEXT NOT NULL REFERENCES mirror_runs(id) ON DELETE CASCADE,
    section                TEXT NOT NULL,
    -- Stable across regeneration: derived from what the conclusion is about,
    -- never from row ids or the run it was generated in. This is what a user
    -- edit and a user deletion attach to.
    fingerprint            TEXT NOT NULL,
    display_rank           INTEGER NOT NULL DEFAULT 0,
    title                  TEXT NOT NULL,
    detail                 TEXT NOT NULL DEFAULT '',
    -- Section-specific: a value's kind, a goal's status, a question's tension.
    status                 TEXT NOT NULL DEFAULT '',
    confidence             REAL NOT NULL DEFAULT 0,
    -- Stated in words, because a number alone never says what is unknown.
    uncertainty            TEXT NOT NULL DEFAULT '',
    evidence_count         INTEGER NOT NULL DEFAULT 0,
    counterevidence_count  INTEGER NOT NULL DEFAULT 0,
    first_seen             TEXT,
    last_seen              TEXT,
    span_days              INTEGER NOT NULL DEFAULT 0,
    active_days            INTEGER NOT NULL DEFAULT 0,
    sources_json           TEXT NOT NULL DEFAULT '[]',
    -- Measured per-conclusion signals: a person's reciprocity, a goal's last
    -- movement. Kept as data so nothing is reduced to one score.
    facets_json            TEXT NOT NULL DEFAULT '{}',
    edited_by_user         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, fingerprint)
);
CREATE INDEX idx_mirror_conclusions_section
    ON mirror_conclusions (run_id, section, display_rank);

CREATE TABLE mirror_conclusion_evidence (
    id             TEXT PRIMARY KEY,
    conclusion_id  TEXT NOT NULL REFERENCES mirror_conclusions(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,      -- supports | contradicts
    claim_id       TEXT,
    entry_id       TEXT NOT NULL,
    quote          TEXT NOT NULL,
    source         TEXT NOT NULL,
    reference      TEXT,
    occurred       TEXT,
    sensitivity    TEXT NOT NULL DEFAULT 'unclassified'
);
CREATE INDEX idx_mirror_evidence_conclusion
    ON mirror_conclusion_evidence (conclusion_id, role);

-- Survives regeneration by design. An edit replaces the displayed text; a
-- deletion keeps the conclusion out of the Mirror until the user restores it.
-- Neither touches the imported evidence underneath.
CREATE TABLE mirror_edits (
    fingerprint  TEXT PRIMARY KEY,
    section      TEXT NOT NULL,
    action       TEXT NOT NULL,        -- edited | deleted
    title        TEXT,
    detail       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
