-- Durable orchestration state for the personal-model compiler. Canonical model
-- records remain in 0020; these tables retain deterministic stage progress and
-- typed compiler outputs which are not themselves knowledge records.

CREATE TABLE personal_model_compiler_checkpoints (
    generation_id TEXT NOT NULL
                  REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    stage          TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('pending','running','complete','failed','stale')),
    input_hash     TEXT NOT NULL,
    detail_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    started_at     TEXT,
    completed_at   TEXT,
    PRIMARY KEY (generation_id, stage)
);
CREATE INDEX idx_personal_model_compiler_checkpoint_state
    ON personal_model_compiler_checkpoints (generation_id, state, stage);

CREATE TABLE personal_model_target_episode_coverage (
    generation_id       TEXT NOT NULL
                        REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    target_entry_id      TEXT NOT NULL,
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    episode_id           TEXT NOT NULL,
    context_complete     INTEGER NOT NULL CHECK (context_complete IN (0,1)),
    correlation_id       TEXT,
    omitted_ranges_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(omitted_ranges_json)),
    PRIMARY KEY (generation_id, target_entry_id, ordinal),
    FOREIGN KEY (generation_id, episode_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX idx_personal_model_target_episode
    ON personal_model_target_episode_coverage (generation_id, episode_id, target_entry_id);

CREATE TABLE personal_model_action_handoffs (
    generation_id       TEXT NOT NULL
                        REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    candidate_id        TEXT NOT NULL,
    title               TEXT NOT NULL,
    node_kind           TEXT NOT NULL,
    target_layer        TEXT NOT NULL,
    observation_ids_json TEXT NOT NULL CHECK (json_valid(observation_ids_json)),
    claim_ids_json       TEXT NOT NULL CHECK (json_valid(claim_ids_json)),
    source_entry_ids_json TEXT NOT NULL CHECK (json_valid(source_entry_ids_json)),
    project_ids_json     TEXT NOT NULL CHECK (json_valid(project_ids_json)),
    parent_goal_ids_json TEXT NOT NULL CHECK (json_valid(parent_goal_ids_json)),
    PRIMARY KEY (generation_id, candidate_id)
);
CREATE INDEX idx_personal_model_action_handoff_project
    ON personal_model_action_handoffs (generation_id, target_layer, candidate_id);

CREATE TABLE personal_model_relationship_histories (
    generation_id       TEXT NOT NULL
                        REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    snapshot_id         TEXT NOT NULL,
    archive_owner_person_id TEXT NOT NULL,
    other_person_id     TEXT NOT NULL,
    period_start        TEXT NOT NULL,
    period_end          TEXT NOT NULL,
    fact_ids_json       TEXT NOT NULL CHECK (json_valid(fact_ids_json)),
    preference_ids_json TEXT NOT NULL CHECK (json_valid(preference_ids_json)),
    shared_event_ids_json TEXT NOT NULL CHECK (json_valid(shared_event_ids_json)),
    commitment_observation_ids_json TEXT NOT NULL CHECK (json_valid(commitment_observation_ids_json)),
    relationship_role_observation_ids_json TEXT NOT NULL CHECK (json_valid(relationship_role_observation_ids_json)),
    PRIMARY KEY (generation_id, snapshot_id),
    FOREIGN KEY (generation_id, snapshot_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE,
    FOREIGN KEY (generation_id, archive_owner_person_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE,
    FOREIGN KEY (generation_id, other_person_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX idx_personal_model_relationship_history_people
    ON personal_model_relationship_histories
       (generation_id, other_person_id, period_start, period_end);

CREATE TABLE personal_model_offered_questions (
    generation_id    TEXT NOT NULL
                     REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    question_id      TEXT NOT NULL,
    interpretation_id TEXT NOT NULL,
    display_text     TEXT NOT NULL,
    normalized_text  TEXT NOT NULL,
    offered_at       TEXT NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('active','withdrawn')),
    PRIMARY KEY (generation_id, question_id),
    UNIQUE (generation_id, interpretation_id, normalized_text),
    FOREIGN KEY (generation_id, interpretation_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX idx_personal_model_offered_questions_active
    ON personal_model_offered_questions
       (generation_id, state, normalized_text, question_id);

CREATE TABLE personal_model_relationship_history_dependencies (
    generation_id TEXT NOT NULL,
    snapshot_id   TEXT NOT NULL,
    ordinal       INTEGER NOT NULL CHECK (ordinal >= 0),
    record_kind   TEXT NOT NULL,
    record_id     TEXT NOT NULL,
    dependency_hash TEXT NOT NULL,
    PRIMARY KEY (generation_id, snapshot_id, ordinal),
    UNIQUE (generation_id, snapshot_id, record_kind, record_id),
    FOREIGN KEY (generation_id, snapshot_id)
        REFERENCES personal_model_relationship_histories(generation_id, snapshot_id)
        ON DELETE CASCADE,
    FOREIGN KEY (generation_id, record_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);

-- These snapshots bind durable source membership to generated identities. They
-- are the replay substrate for merge/split corrections across rebuilds.
CREATE TABLE personal_model_source_identity_memberships (
    generation_id       TEXT NOT NULL
                        REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    person_id            TEXT NOT NULL,
    source_identity_key  TEXT NOT NULL,
    source               TEXT NOT NULL,
    source_account       TEXT NOT NULL,
    source_key           TEXT NOT NULL,
    membership_hash      TEXT NOT NULL,
    PRIMARY KEY (generation_id, source_identity_key),
    FOREIGN KEY (generation_id, person_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX idx_personal_model_source_membership_person
    ON personal_model_source_identity_memberships (generation_id, person_id, source_identity_key);

CREATE TABLE personal_model_project_candidate_memberships (
    generation_id       TEXT NOT NULL
                        REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    project_id           TEXT NOT NULL,
    candidate_id         TEXT NOT NULL,
    membership_hash      TEXT NOT NULL,
    PRIMARY KEY (generation_id, candidate_id),
    FOREIGN KEY (generation_id, project_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX idx_personal_model_project_membership_project
    ON personal_model_project_candidate_memberships (generation_id, project_id, candidate_id);

CREATE TABLE personal_model_goal_candidate_memberships (
    generation_id       TEXT NOT NULL
                        REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    goal_id              TEXT NOT NULL,
    candidate_id         TEXT NOT NULL,
    membership_hash      TEXT NOT NULL,
    PRIMARY KEY (generation_id, candidate_id),
    FOREIGN KEY (generation_id, goal_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX idx_personal_model_goal_membership_goal
    ON personal_model_goal_candidate_memberships (generation_id, goal_id, candidate_id);

-- Cache only the closed-schema annotation response. The key binds every source,
-- episode, identity-resolution, schema, provider/model and prompt input.
CREATE TABLE personal_model_observation_annotation_cache (
    cache_key             TEXT PRIMARY KEY,
    dependency_fingerprint TEXT NOT NULL,
    resolution_hash       TEXT NOT NULL,
    schema_version        TEXT NOT NULL,
    provider_name         TEXT NOT NULL,
    provider_version      TEXT NOT NULL,
    model_name            TEXT NOT NULL,
    prompt_version        TEXT NOT NULL,
    response_json         TEXT NOT NULL CHECK (json_valid(response_json)),
    created_at            TEXT NOT NULL
);
CREATE INDEX idx_personal_model_observation_cache_dependency
    ON personal_model_observation_annotation_cache
       (dependency_fingerprint, resolution_hash, schema_version, provider_name,
        provider_version, model_name, prompt_version);

-- Project history is a fact projection. These predicates are intentionally
-- first-class and indexable rather than hidden inside project display JSON.
CREATE INDEX idx_personal_model_project_history
    ON personal_model_records
       (generation_id, normalized_predicate, entity_kind, entity_id, valid_from, valid_to)
    WHERE record_kind='fact'
      AND normalized_predicate IN ('project.purpose','goal.status');

-- Everything generation-scoped becomes immutable at activation.
CREATE TRIGGER personal_model_compiler_checkpoints_activated_no_insert
BEFORE INSERT ON personal_model_compiler_checkpoints
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated compiler checkpoints are immutable'); END;
CREATE TRIGGER personal_model_compiler_checkpoints_activated_no_update
BEFORE UPDATE ON personal_model_compiler_checkpoints
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated compiler checkpoints are immutable'); END;
CREATE TRIGGER personal_model_compiler_checkpoints_activated_no_delete
BEFORE DELETE ON personal_model_compiler_checkpoints
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated compiler checkpoints are immutable'); END;

CREATE TRIGGER personal_model_coverage_activated_no_insert BEFORE INSERT ON personal_model_target_episode_coverage
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated target coverage is immutable'); END;
CREATE TRIGGER personal_model_coverage_activated_no_update BEFORE UPDATE ON personal_model_target_episode_coverage
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated target coverage is immutable'); END;
CREATE TRIGGER personal_model_coverage_activated_no_delete BEFORE DELETE ON personal_model_target_episode_coverage
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated target coverage is immutable'); END;

CREATE TRIGGER personal_model_handoffs_activated_no_insert BEFORE INSERT ON personal_model_action_handoffs
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated action handoffs are immutable'); END;
CREATE TRIGGER personal_model_handoffs_activated_no_update BEFORE UPDATE ON personal_model_action_handoffs
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated action handoffs are immutable'); END;
CREATE TRIGGER personal_model_handoffs_activated_no_delete BEFORE DELETE ON personal_model_action_handoffs
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated action handoffs are immutable'); END;

CREATE TRIGGER personal_model_histories_activated_no_insert BEFORE INSERT ON personal_model_relationship_histories
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated relationship histories are immutable'); END;
CREATE TRIGGER personal_model_histories_activated_no_update BEFORE UPDATE ON personal_model_relationship_histories
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated relationship histories are immutable'); END;
CREATE TRIGGER personal_model_histories_activated_no_delete BEFORE DELETE ON personal_model_relationship_histories
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated relationship histories are immutable'); END;
CREATE TRIGGER personal_model_history_deps_activated_no_insert BEFORE INSERT ON personal_model_relationship_history_dependencies
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated relationship history dependencies are immutable'); END;
CREATE TRIGGER personal_model_history_deps_activated_no_update BEFORE UPDATE ON personal_model_relationship_history_dependencies
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated relationship history dependencies are immutable'); END;
CREATE TRIGGER personal_model_history_deps_activated_no_delete BEFORE DELETE ON personal_model_relationship_history_dependencies
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated relationship history dependencies are immutable'); END;

CREATE TRIGGER personal_model_questions_activated_no_insert BEFORE INSERT ON personal_model_offered_questions
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated offered questions are immutable'); END;
CREATE TRIGGER personal_model_questions_activated_no_update BEFORE UPDATE ON personal_model_offered_questions
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated offered questions are immutable'); END;
CREATE TRIGGER personal_model_questions_activated_no_delete BEFORE DELETE ON personal_model_offered_questions
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated offered questions are immutable'); END;

CREATE TRIGGER personal_model_source_memberships_activated_no_insert BEFORE INSERT ON personal_model_source_identity_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated source memberships are immutable'); END;
CREATE TRIGGER personal_model_source_memberships_activated_no_update BEFORE UPDATE ON personal_model_source_identity_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated source memberships are immutable'); END;
CREATE TRIGGER personal_model_source_memberships_activated_no_delete BEFORE DELETE ON personal_model_source_identity_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated source memberships are immutable'); END;

CREATE TRIGGER personal_model_project_memberships_activated_no_insert BEFORE INSERT ON personal_model_project_candidate_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated project memberships are immutable'); END;
CREATE TRIGGER personal_model_project_memberships_activated_no_update BEFORE UPDATE ON personal_model_project_candidate_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated project memberships are immutable'); END;
CREATE TRIGGER personal_model_project_memberships_activated_no_delete BEFORE DELETE ON personal_model_project_candidate_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated project memberships are immutable'); END;

CREATE TRIGGER personal_model_goal_memberships_activated_no_insert BEFORE INSERT ON personal_model_goal_candidate_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated goal memberships are immutable'); END;
CREATE TRIGGER personal_model_goal_memberships_activated_no_update BEFORE UPDATE ON personal_model_goal_candidate_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated goal memberships are immutable'); END;
CREATE TRIGGER personal_model_goal_memberships_activated_no_delete BEFORE DELETE ON personal_model_goal_candidate_memberships
WHEN EXISTS (SELECT 1 FROM personal_model_generations g WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated goal memberships are immutable'); END;
