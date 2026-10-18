-- Add the interpretation verdict ledger action without weakening the existing
-- append-only checks or discarding historical replay audit rows.

CREATE TABLE personal_model_overrides_v2 (
    sequence                INTEGER PRIMARY KEY AUTOINCREMENT,
    id                      TEXT NOT NULL UNIQUE,
    target_kind             TEXT NOT NULL CHECK (target_kind IN
                             ('person','project','fact','preference','goal','interpretation')),
    target_record_id        TEXT NOT NULL,
    action                  TEXT NOT NULL CHECK (action IN
                             ('merge','split','confirm','edit','reject','supersede',
                              'reparent','reclassify','dispute','delete','restore','verdict')),
    payload_json            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    struck_value_json       TEXT CHECK
                             (struck_value_json IS NULL OR json_valid(struck_value_json)),
    supersedes_override_id  TEXT REFERENCES personal_model_overrides_v2(id)
                             ON DELETE RESTRICT,
    actor                   TEXT NOT NULL DEFAULT 'owner',
    created_at              TEXT NOT NULL,
    UNIQUE (supersedes_override_id)
);

INSERT INTO personal_model_overrides_v2
    (sequence,id,target_kind,target_record_id,action,payload_json,
     struck_value_json,supersedes_override_id,actor,created_at)
SELECT sequence,id,target_kind,target_record_id,action,payload_json,
       struck_value_json,supersedes_override_id,actor,created_at
FROM personal_model_overrides;

CREATE TABLE personal_model_override_replays_v2 (
    generation_id     TEXT NOT NULL
                      REFERENCES personal_model_generations(id) ON DELETE CASCADE,
    override_id       TEXT NOT NULL
                      REFERENCES personal_model_overrides_v2(id) ON DELETE RESTRICT,
    replay_order      INTEGER NOT NULL CHECK (replay_order >= 1),
    outcome           TEXT NOT NULL CHECK (outcome IN
                        ('applied','not_applicable','orphaned','conflict')),
    matched_record_id TEXT,
    detail            TEXT NOT NULL DEFAULT '',
    replayed_at       TEXT NOT NULL,
    PRIMARY KEY (generation_id, override_id),
    UNIQUE (generation_id, replay_order)
);

INSERT INTO personal_model_override_replays_v2
    (generation_id,override_id,replay_order,outcome,matched_record_id,detail,replayed_at)
SELECT generation_id,override_id,replay_order,outcome,matched_record_id,detail,replayed_at
FROM personal_model_override_replays;

DROP TABLE personal_model_override_replays;
DROP TABLE personal_model_overrides;
ALTER TABLE personal_model_overrides_v2 RENAME TO personal_model_overrides;
ALTER TABLE personal_model_override_replays_v2 RENAME TO personal_model_override_replays;

CREATE INDEX idx_personal_model_overrides_target
    ON personal_model_overrides (target_kind, target_record_id, sequence);
CREATE INDEX idx_personal_model_override_replays_outcome
    ON personal_model_override_replays (generation_id, outcome, replay_order);

CREATE TRIGGER personal_model_overrides_no_update
BEFORE UPDATE ON personal_model_overrides
BEGIN SELECT RAISE(ABORT, 'personal-model overrides are append-only'); END;
CREATE TRIGGER personal_model_overrides_no_delete
BEFORE DELETE ON personal_model_overrides
BEGIN SELECT RAISE(ABORT, 'personal-model overrides are append-only'); END;

CREATE TRIGGER personal_model_replays_activated_no_update
BEFORE UPDATE ON personal_model_override_replays
WHEN EXISTS (SELECT 1 FROM personal_model_generations g
             WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated personal-model override replay is immutable'); END;
CREATE TRIGGER personal_model_replays_activated_no_insert
BEFORE INSERT ON personal_model_override_replays
WHEN EXISTS (SELECT 1 FROM personal_model_generations g
             WHERE g.id=NEW.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated personal-model override replay is immutable'); END;
CREATE TRIGGER personal_model_replays_activated_no_delete
BEFORE DELETE ON personal_model_override_replays
WHEN EXISTS (SELECT 1 FROM personal_model_generations g
             WHERE g.id=OLD.generation_id AND g.state IN ('current','superseded'))
BEGIN SELECT RAISE(ABORT, 'activated personal-model override replay is immutable'); END;
