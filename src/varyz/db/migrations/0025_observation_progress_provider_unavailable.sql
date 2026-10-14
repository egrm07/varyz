-- A run can also stop because the annotator went away: a local server that died
-- or hung answers nothing, and reading on would mark the archive as read while
-- producing no observations. That stop is recorded like the other two, so a
-- resumed run can say why the last one ended and a supervisor can tell a dead
-- provider (restart it) from a spent budget (do not).

CREATE TABLE personal_model_observation_progress_v2 (
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
    budget_exhausted_by        TEXT CHECK (budget_exhausted_by IN
                                ('call_budget','deadline','provider_unavailable')),
    annotated_episode_ids_json TEXT NOT NULL DEFAULT '[]'
                               CHECK (json_valid(annotated_episode_ids_json)),
    reached_entry_ids_json     TEXT NOT NULL DEFAULT '[]'
                               CHECK (json_valid(reached_entry_ids_json)),
    updated_at                 TEXT NOT NULL,
    CHECK (episodes_annotated <= episodes_total),
    CHECK (episodes_remaining = episodes_total - episodes_annotated),
    CHECK ((state = 'partial') = (budget_exhausted_by IS NOT NULL))
);

INSERT INTO personal_model_observation_progress_v2
    (generation_id,state,episodes_annotated,episodes_total,episodes_remaining,
     entries_reached,annotation_calls,cache_hits,max_annotation_calls,deadline,
     budget_exhausted_by,annotated_episode_ids_json,reached_entry_ids_json,updated_at)
SELECT generation_id,state,episodes_annotated,episodes_total,episodes_remaining,
       entries_reached,annotation_calls,cache_hits,max_annotation_calls,deadline,
       budget_exhausted_by,annotated_episode_ids_json,reached_entry_ids_json,updated_at
FROM personal_model_observation_progress;

DROP TRIGGER IF EXISTS personal_model_observation_progress_activated_no_insert;
DROP TRIGGER IF EXISTS personal_model_observation_progress_activated_no_update;
DROP TRIGGER IF EXISTS personal_model_observation_progress_activated_no_delete;
DROP INDEX IF EXISTS idx_personal_model_observation_progress_state;
DROP TABLE personal_model_observation_progress;

ALTER TABLE personal_model_observation_progress_v2
    RENAME TO personal_model_observation_progress;

CREATE INDEX idx_personal_model_observation_progress_state
    ON personal_model_observation_progress (state, updated_at);

CREATE TRIGGER personal_model_observation_progress_activated_no_insert
BEFORE INSERT ON personal_model_observation_progress
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations generation
    WHERE generation.id=NEW.generation_id AND generation.state IN ('current','superseded')
)
BEGIN SELECT RAISE(ABORT, 'activated observation progress is immutable'); END;

CREATE TRIGGER personal_model_observation_progress_activated_no_update
BEFORE UPDATE ON personal_model_observation_progress
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations generation
    WHERE generation.id=OLD.generation_id AND generation.state IN ('current','superseded')
)
BEGIN SELECT RAISE(ABORT, 'activated observation progress is immutable'); END;

CREATE TRIGGER personal_model_observation_progress_activated_no_delete
BEFORE DELETE ON personal_model_observation_progress
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations generation
    WHERE generation.id=OLD.generation_id AND generation.state IN ('current','superseded')
)
BEGIN SELECT RAISE(ABORT, 'activated observation progress is immutable'); END;
