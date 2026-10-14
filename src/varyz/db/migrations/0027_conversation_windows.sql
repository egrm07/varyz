-- Conversation windows: the unit a chat archive can actually be read in.
--
-- An entry from a messaging source is a single message, and on this machine the
-- median one is 47 characters long. Embedded alone it is close to noise, BM25
-- has almost nothing to weigh, and a claim extracted from it is a restatement
-- of it rather than a conclusion about it. The thing a person remembers is the
-- conversation, so that is what gets indexed, labelled and summarised.
--
-- The split is structural and deterministic -- same thread, no gap longer than
-- the policy allows, capped in turns and characters -- so a window is a fact
-- about the archive rather than an opinion of a model. `id` is the content
-- hash of the ordered membership plus the policy version, which means a
-- changed policy produces different windows instead of silently reinterpreting
-- the stored ones, and re-running the partition is idempotent.
CREATE TABLE conversation_windows (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    thread_id       TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT NOT NULL,
    turns           INTEGER NOT NULL CHECK (turns > 0),
    characters      INTEGER NOT NULL CHECK (characters >= 0),
    policy_version  TEXT NOT NULL,
    -- Over the members' content hashes, not their text: an edited or re-ingested
    -- message changes this, and everything derived from the window goes stale
    -- with it.
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_conversation_windows_thread
    ON conversation_windows (source, thread_id, started_at);

CREATE TABLE conversation_window_entries (
    window_id  TEXT NOT NULL REFERENCES conversation_windows (id) ON DELETE CASCADE,
    entry_id   TEXT NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    PRIMARY KEY (window_id, entry_id)
);

CREATE INDEX idx_conversation_window_entries_entry
    ON conversation_window_entries (entry_id);

-- What a window is allowed to be, decided by the local model and nothing else.
--
-- Source defaults made every message from a chat connector sensitive, which is
-- the right fail-closed answer in the absence of any other, and the reason 91%
-- of this archive reaches no agent at all. A per-window decision replaces the
-- per-source one. It is stored apart from the window because the window is
-- structural and this is a judgement: re-running the judgement must not be able
-- to disturb the partition, and dropping this table must leave the archive as
-- it was.
--
-- `agreed` records whether every voter reached the same answer. A disagreement
-- is stored as sensitive with agreed = 0, because the only safe reading of a
-- split vote is the cautious one.
CREATE TABLE conversation_window_labels (
    window_id       TEXT PRIMARY KEY REFERENCES conversation_windows (id) ON DELETE CASCADE,
    label           TEXT NOT NULL CHECK (label IN ('personal', 'sensitive')),
    agreed          INTEGER NOT NULL CHECK (agreed IN (0, 1)),
    votes_json      TEXT NOT NULL CHECK (json_valid(votes_json)),
    prompt_version  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- The conclusions a window is worth remembering for.
--
-- These are written to travel where the messages must not: a sentence that
-- stands on its own, naming who and when, carrying no quoted wording. That is
-- the difference between this table and `claims`, whose object on this archive
-- has a median length of 38 characters against an evidence quote's 36 -- a
-- paraphrase of one short message rather than an abstraction over a
-- conversation.
--
-- `sensitivity` is the window's label at the time of writing, copied here so a
-- reader gating on it needs one table; the label table stays the source of
-- truth. Most windows produce nothing, and an empty result is a real result:
-- absence is recorded by the run, not by a row.
CREATE TABLE conversation_window_conclusions (
    id              TEXT PRIMARY KEY,
    window_id       TEXT NOT NULL REFERENCES conversation_windows (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    text            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    sensitivity     TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (window_id, seq)
);

CREATE INDEX idx_conversation_window_conclusions_window
    ON conversation_window_conclusions (window_id);

-- One row per window the model has finished with, whatever it produced.
--
-- Without it a window that honestly holds nothing worth remembering is
-- indistinguishable from one never read, and every re-run pays for it again.
-- Roughly four windows in five are expected to be of that kind.
CREATE TABLE conversation_window_runs (
    window_id       TEXT NOT NULL REFERENCES conversation_windows (id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    conclusions     INTEGER NOT NULL CHECK (conclusions >= 0),
    created_at      TEXT NOT NULL,
    PRIMARY KEY (window_id, model, prompt_version)
);
