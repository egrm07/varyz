-- Observation inference is intentionally resumable through the cross-generation
-- annotation cache. This table makes each run's coverage and stopping reason visible.

CREATE TABLE IF NOT EXISTS personal_model_observation_progress (
    generation_id              TEXT PRIMARY KEY
                               REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    state                      TEXT NOT NULL CHECK (state IN ('partial','complete')),
    episodes_annotated         INTEGER NOT NULL CHECK (episodes_annotated >= 0),
    episodes_total             INTEGER NOT NULL CHECK (episodes_total >= 0),
    episodes_remaining         INTEGER NOT NULL CHECK (episodes_remaining >= 0),
    entries_reached            INTEGER NOT NULL CHECK (entries_reached >= 0),
    annotation_calls           INTEGER NOT NULL CHECK (annotation_calls >= 0),
    cache_hits                 INTEGER NOT NULL CHECK (cache_hits >= 0),
    max_annotation_calls       INTEGER CHECK (max_annotation_calls >= 0),
    deadline                   TEXT,
    budget_exhausted_by        TEXT CHECK (budget_exhausted_by IN ('call_budget','deadline')),
    annotated_episode_ids_json TEXT NOT NULL DEFAULT '[]'
                               CHECK (json_valid(annotated_episode_ids_json)),
    reached_entry_ids_json     TEXT NOT NULL DEFAULT '[]'
                               CHECK (json_valid(reached_entry_ids_json)),
    updated_at                 TEXT NOT NULL,
    CHECK (episodes_annotated <= episodes_total),
    CHECK (episodes_remaining = episodes_total - episodes_annotated),
    CHECK ((state = 'partial') = (budget_exhausted_by IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_personal_model_observation_progress_state
    ON personal_model_observation_progress (state, updated_at);

CREATE TRIGGER IF NOT EXISTS personal_model_observation_progress_activated_no_insert
BEFORE INSERT ON personal_model_observation_progress
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations generation
    WHERE generation.id=NEW.generation_id AND generation.state IN ('current','superseded')
)
BEGIN SELECT RAISE(ABORT, 'activated observation progress is immutable'); END;

CREATE TRIGGER IF NOT EXISTS personal_model_observation_progress_activated_no_update
BEFORE UPDATE ON personal_model_observation_progress
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations generation
    WHERE generation.id=OLD.generation_id AND generation.state IN ('current','superseded')
)
BEGIN SELECT RAISE(ABORT, 'activated observation progress is immutable'); END;

CREATE TRIGGER IF NOT EXISTS personal_model_observation_progress_activated_no_delete
BEFORE DELETE ON personal_model_observation_progress
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations generation
    WHERE generation.id=OLD.generation_id AND generation.state IN ('current','superseded')
)
BEGIN SELECT RAISE(ABORT, 'activated observation progress is immutable'); END;
