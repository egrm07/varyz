-- Canonical personal-model generations. Atomic graph rows remain evidence-level;
-- these tables store the compiled, user-correctable model projected by Mirror/MCP.

CREATE TABLE personal_model_generations (
    id                     TEXT PRIMARY KEY,
    graph_generation_id    TEXT NOT NULL REFERENCES graph_runs(id) ON DELETE RESTRICT,
    compiler_version       TEXT NOT NULL,
    policy_version         TEXT NOT NULL,
    input_content_hash     TEXT NOT NULL,
    state                  TEXT NOT NULL CHECK (state IN
                               ('staged','current','superseded','failed')),
    -- Overrides are append-only. A build replays the exact prefix visible when
    -- it began, while overrides appended later belong to the next generation.
    override_sequence_max  INTEGER NOT NULL DEFAULT 0 CHECK (override_sequence_max >= 0),
    created_at             TEXT NOT NULL,
    completed_at           TEXT,
    activated_at           TEXT,
    failed_at              TEXT,
    failure_reason         TEXT,
    record_count           INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
    reused_record_count    INTEGER NOT NULL DEFAULT 0 CHECK (reused_record_count >= 0),
    quarantine_count       INTEGER NOT NULL DEFAULT 0 CHECK (quarantine_count >= 0),
    metrics_json           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metrics_json)),
    CHECK (state <> 'failed' OR failed_at IS NOT NULL),
    CHECK (state NOT IN ('current','superseded') OR activated_at IS NOT NULL)
);
CREATE UNIQUE INDEX idx_personal_model_one_current
    ON personal_model_generations (state) WHERE state = 'current';
CREATE INDEX idx_personal_model_generations_graph
    ON personal_model_generations (graph_generation_id, state, created_at);

-- A singleton pointer is the read boundary. Readers never infer "current" by
-- sorting timestamps, and activation changes this pointer in the same transaction
-- that supersedes the prior generation.
CREATE TABLE personal_model_active_generation (
    singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation_id  TEXT NOT NULL UNIQUE
                   REFERENCES personal_model_generations(id) ON DELETE RESTRICT,
    activated_at   TEXT NOT NULL
);

CREATE TRIGGER personal_model_active_requires_current_insert
BEFORE INSERT ON personal_model_active_generation
WHEN NOT EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state = 'current'
)
BEGIN
    SELECT RAISE(ABORT, 'active personal-model pointer requires a current generation');
END;

CREATE TRIGGER personal_model_active_requires_current_update
BEFORE UPDATE OF generation_id ON personal_model_active_generation
WHEN NOT EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state = 'current'
)
BEGIN
    SELECT RAISE(ABORT, 'active personal-model pointer requires a current generation');
END;

CREATE TRIGGER personal_model_pointed_generation_must_remain_current
BEFORE UPDATE OF state ON personal_model_generations
WHEN NEW.state <> 'current' AND EXISTS (
    SELECT 1 FROM personal_model_active_generation active
    WHERE active.singleton = 1 AND active.generation_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'active personal-model pointer cannot name a non-current generation');
END;

CREATE TABLE personal_model_records (
    generation_id       TEXT NOT NULL
                        REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    record_id            TEXT NOT NULL,
    record_kind          TEXT NOT NULL CHECK (record_kind IN
                           ('conversation_episode','observation','person','project','goal',
                            'fact','preference','event','relationship_snapshot',
                            'interpretation_candidate','interpretation')),
    record_json          TEXT NOT NULL CHECK (json_valid(record_json)),
    canonical_json_hash  TEXT NOT NULL,
    dependency_hash      TEXT NOT NULL,
    -- Query/index projections. The validated JSON remains canonical.
    entity_kind          TEXT,
    entity_id            TEXT,
    normalized_predicate TEXT,
    normalized_concept   TEXT,
    readiness            TEXT NOT NULL CHECK (readiness IN
                           ('staged','ready','limited','quarantined','rejected',
                            'superseded')),
    valid_from           TEXT,
    valid_to             TEXT,
    sensitivity          TEXT NOT NULL CHECK (sensitivity IN
                           ('public','personal','sensitive','unclassified')),
    created_at           TEXT NOT NULL,
    reused_from_generation_id TEXT,
    PRIMARY KEY (generation_id, record_id),
    UNIQUE (generation_id, record_kind, record_id),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
    CHECK (reused_from_generation_id IS NULL OR reused_from_generation_id <> generation_id)
);
CREATE INDEX idx_personal_model_records_kind
    ON personal_model_records (generation_id, record_kind, readiness);
CREATE INDEX idx_personal_model_records_entity
    ON personal_model_records (generation_id, entity_kind, entity_id, record_kind);
CREATE INDEX idx_personal_model_records_predicate
    ON personal_model_records
       (generation_id, normalized_predicate, readiness, valid_from, valid_to);
CREATE INDEX idx_personal_model_records_concept
    ON personal_model_records (generation_id, normalized_concept, readiness);
CREATE INDEX idx_personal_model_records_sensitivity
    ON personal_model_records (generation_id, sensitivity, readiness);
CREATE INDEX idx_personal_model_records_dependency
    ON personal_model_records (record_kind, record_id, dependency_hash, generation_id);

CREATE TABLE personal_model_record_evidence (
    generation_id        TEXT NOT NULL,
    record_id             TEXT NOT NULL,
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    entry_id              TEXT NOT NULL,
    role                  TEXT NOT NULL,
    claim_id              TEXT,
    episode_id            TEXT,
    observation_id        TEXT,
    independence_group_id TEXT,
    reference_json        TEXT NOT NULL CHECK (json_valid(reference_json)),
    PRIMARY KEY (generation_id, record_id, ordinal),
    FOREIGN KEY (generation_id, record_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX idx_personal_model_evidence_entry
    ON personal_model_record_evidence (entry_id, generation_id, record_id);
CREATE INDEX idx_personal_model_evidence_claim
    ON personal_model_record_evidence (claim_id, generation_id, record_id);
CREATE INDEX idx_personal_model_evidence_episode
    ON personal_model_record_evidence (episode_id, generation_id, record_id);

CREATE TABLE personal_model_record_claims (
    generation_id  TEXT NOT NULL,
    record_id       TEXT NOT NULL,
    ordinal         INTEGER NOT NULL CHECK (ordinal >= 0),
    claim_id        TEXT NOT NULL,
    PRIMARY KEY (generation_id, record_id, ordinal),
    UNIQUE (generation_id, record_id, claim_id),
    FOREIGN KEY (generation_id, record_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX idx_personal_model_claim_membership
    ON personal_model_record_claims (claim_id, generation_id, record_id);

CREATE TABLE personal_model_record_dependencies (
    generation_id       TEXT NOT NULL,
    record_id            TEXT NOT NULL,
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    dependency_kind      TEXT NOT NULL,
    dependency_id        TEXT NOT NULL,
    dependency_hash      TEXT NOT NULL,
    -- NULL denotes graph/evidence/config input; a value denotes another model row
    -- and must equal generation_id. Repository activation validates the target kind.
    target_generation_id TEXT,
    PRIMARY KEY (generation_id, record_id, ordinal),
    UNIQUE (generation_id, record_id, dependency_kind, dependency_id),
    FOREIGN KEY (generation_id, record_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE,
    CHECK (target_generation_id IS NULL OR target_generation_id = generation_id)
);
CREATE INDEX idx_personal_model_dependency_member
    ON personal_model_record_dependencies
       (dependency_kind, dependency_id, dependency_hash, generation_id);

CREATE TABLE personal_model_quarantines (
    id             TEXT PRIMARY KEY,
    generation_id  TEXT NOT NULL
                   REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    stage          TEXT NOT NULL,
    record_kind    TEXT,
    candidate_id   TEXT,
    reason_code    TEXT NOT NULL,
    detail_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    created_at     TEXT NOT NULL
);
CREATE INDEX idx_personal_model_quarantines_run
    ON personal_model_quarantines (generation_id, stage, reason_code);

CREATE TABLE personal_model_run_metrics (
    generation_id  TEXT NOT NULL
                   REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    stage          TEXT NOT NULL,
    metric_name    TEXT NOT NULL,
    metric_json    TEXT NOT NULL CHECK (json_valid(metric_json)),
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (generation_id, stage, metric_name)
);

-- Corrections never mutate imported evidence or activated model rows. Later
-- overrides may supersede earlier ledger entries, but neither is deleted.
CREATE TABLE personal_model_overrides (
    sequence                INTEGER PRIMARY KEY AUTOINCREMENT,
    id                      TEXT NOT NULL UNIQUE,
    target_kind             TEXT NOT NULL CHECK (target_kind IN
                             ('person','project','fact','preference','goal','interpretation')),
    target_record_id        TEXT NOT NULL,
    action                  TEXT NOT NULL CHECK (action IN
                             ('merge','split','confirm','edit','reject','supersede',
                              'reparent','reclassify','dispute','delete','restore')),
    payload_json            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    struck_value_json       TEXT CHECK
                             (struck_value_json IS NULL OR json_valid(struck_value_json)),
    supersedes_override_id  TEXT REFERENCES personal_model_overrides(id) ON DELETE RESTRICT,
    actor                   TEXT NOT NULL DEFAULT 'owner',
    created_at              TEXT NOT NULL,
    UNIQUE (supersedes_override_id)
);
CREATE INDEX idx_personal_model_overrides_target
    ON personal_model_overrides (target_kind, target_record_id, sequence);

CREATE TABLE personal_model_override_replays (
    generation_id    TEXT NOT NULL
                     REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    override_id      TEXT NOT NULL
                     REFERENCES personal_model_overrides(id) ON DELETE RESTRICT,
    replay_order     INTEGER NOT NULL CHECK (replay_order >= 1),
    outcome          TEXT NOT NULL CHECK (outcome IN
                       ('applied','not_applicable','orphaned','conflict')),
    matched_record_id TEXT,
    detail           TEXT NOT NULL DEFAULT '',
    replayed_at      TEXT NOT NULL,
    PRIMARY KEY (generation_id, override_id),
    UNIQUE (generation_id, replay_order)
);
CREATE INDEX idx_personal_model_override_replays_outcome
    ON personal_model_override_replays (generation_id, outcome, replay_order);

-- Activated generations are append-only snapshots, including provenance and
-- replay accounting. Staged/failed generations may be repaired or removed.
CREATE TRIGGER personal_model_records_activated_no_insert
BEFORE INSERT ON personal_model_records
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model records are immutable');
END;

CREATE TRIGGER personal_model_records_activated_no_update
BEFORE UPDATE ON personal_model_records
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model records are immutable');
END;

CREATE TRIGGER personal_model_records_activated_no_delete
BEFORE DELETE ON personal_model_records
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model records are immutable');
END;

CREATE TRIGGER personal_model_evidence_activated_no_update
BEFORE UPDATE ON personal_model_record_evidence
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model evidence is immutable');
END;

CREATE TRIGGER personal_model_evidence_activated_no_insert
BEFORE INSERT ON personal_model_record_evidence
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model evidence is immutable');
END;

CREATE TRIGGER personal_model_evidence_activated_no_delete
BEFORE DELETE ON personal_model_record_evidence
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model evidence is immutable');
END;

CREATE TRIGGER personal_model_claims_activated_no_update
BEFORE UPDATE ON personal_model_record_claims
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model claim membership is immutable');
END;

CREATE TRIGGER personal_model_claims_activated_no_insert
BEFORE INSERT ON personal_model_record_claims
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model claim membership is immutable');
END;

CREATE TRIGGER personal_model_claims_activated_no_delete
BEFORE DELETE ON personal_model_record_claims
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model claim membership is immutable');
END;

CREATE TRIGGER personal_model_dependencies_activated_no_update
BEFORE UPDATE ON personal_model_record_dependencies
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model dependencies are immutable');
END;

CREATE TRIGGER personal_model_dependencies_activated_no_insert
BEFORE INSERT ON personal_model_record_dependencies
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model dependencies are immutable');
END;

CREATE TRIGGER personal_model_dependencies_activated_no_delete
BEFORE DELETE ON personal_model_record_dependencies
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model dependencies are immutable');
END;

CREATE TRIGGER personal_model_replays_activated_no_update
BEFORE UPDATE ON personal_model_override_replays
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model override replay is immutable');
END;

CREATE TRIGGER personal_model_replays_activated_no_insert
BEFORE INSERT ON personal_model_override_replays
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model override replay is immutable');
END;

CREATE TRIGGER personal_model_replays_activated_no_delete
BEFORE DELETE ON personal_model_override_replays
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model override replay is immutable');
END;

CREATE TRIGGER personal_model_quarantines_activated_no_insert
BEFORE INSERT ON personal_model_quarantines
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model quarantines are immutable');
END;

CREATE TRIGGER personal_model_quarantines_activated_no_update
BEFORE UPDATE ON personal_model_quarantines
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model quarantines are immutable');
END;

CREATE TRIGGER personal_model_quarantines_activated_no_delete
BEFORE DELETE ON personal_model_quarantines
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model quarantines are immutable');
END;

CREATE TRIGGER personal_model_metrics_activated_no_insert
BEFORE INSERT ON personal_model_run_metrics
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model metrics are immutable');
END;

CREATE TRIGGER personal_model_metrics_activated_no_update
BEFORE UPDATE ON personal_model_run_metrics
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model metrics are immutable');
END;

CREATE TRIGGER personal_model_metrics_activated_no_delete
BEFORE DELETE ON personal_model_run_metrics
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model metrics are immutable');
END;

CREATE TRIGGER personal_model_overrides_no_update
BEFORE UPDATE ON personal_model_overrides
BEGIN
    SELECT RAISE(ABORT, 'personal-model overrides are append-only');
END;

CREATE TRIGGER personal_model_overrides_no_delete
BEFORE DELETE ON personal_model_overrides
BEGIN
    SELECT RAISE(ABORT, 'personal-model overrides are append-only');
END;
