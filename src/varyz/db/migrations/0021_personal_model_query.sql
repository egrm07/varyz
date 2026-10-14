-- Read projections for the typed life-model query service. The JSON snapshot in
-- personal_model_records remains canonical; these rows only make every entity
-- membership and durable alias independently searchable.

CREATE TABLE IF NOT EXISTS personal_model_entity_memberships (
    generation_id  TEXT NOT NULL,
    record_id       TEXT NOT NULL,
    entity_kind     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    role            TEXT NOT NULL,
    PRIMARY KEY (generation_id, record_id, entity_kind, entity_id, role),
    FOREIGN KEY (generation_id, record_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_personal_model_membership_lookup
    ON personal_model_entity_memberships
       (generation_id, entity_kind, entity_id, role, record_id);
CREATE INDEX IF NOT EXISTS idx_personal_model_membership_record
    ON personal_model_entity_memberships (generation_id, record_id);

CREATE TABLE IF NOT EXISTS personal_model_entity_aliases (
    generation_id    TEXT NOT NULL,
    entity_kind      TEXT NOT NULL CHECK (entity_kind IN ('person','project','goal')),
    entity_id        TEXT NOT NULL,
    alias_kind       TEXT NOT NULL,
    exact_value      TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT '',
    source_account   TEXT NOT NULL DEFAULT '',
    verification     TEXT NOT NULL,
    resolution_tier  INTEGER NOT NULL CHECK (resolution_tier BETWEEN 0 AND 2),
    PRIMARY KEY
      (generation_id, entity_kind, entity_id, alias_kind, exact_value,
       source, source_account),
    FOREIGN KEY (generation_id, entity_id)
        REFERENCES personal_model_records(generation_id, record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_personal_model_alias_exact
    ON personal_model_entity_aliases
       (generation_id, exact_value, resolution_tier, entity_kind, entity_id);
CREATE INDEX IF NOT EXISTS idx_personal_model_alias_normalized
    ON personal_model_entity_aliases
       (generation_id, normalized_value, resolution_tier, entity_kind, entity_id);

-- A staged generation is populated incrementally. These triggers never mutate an
-- activated snapshot and therefore preserve the generation read boundary.
CREATE TRIGGER IF NOT EXISTS personal_model_query_membership_after_insert
AFTER INSERT ON personal_model_records
BEGIN
    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, NEW.record_kind, NEW.record_id, 'self'
    WHERE NEW.record_kind IN ('person','project','goal','event');

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id,
           json_extract(NEW.record_json, '$.subject.kind'),
           json_extract(NEW.record_json, '$.subject.id'), 'subject'
    WHERE NEW.record_kind IN ('fact','preference');

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id,
           json_extract(NEW.record_json, '$.subject_kind'),
           json_extract(NEW.record_json, '$.subject_id'), 'subject'
    WHERE NEW.record_kind = 'observation'
      AND json_extract(NEW.record_json, '$.subject_kind') IS NOT NULL
      AND json_extract(NEW.record_json, '$.subject_id') IS NOT NULL;

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'person',
           json_extract(NEW.record_json, '$.speaker_person_id'), 'speaker'
    WHERE NEW.record_kind = 'observation'
      AND json_extract(NEW.record_json, '$.speaker_person_id') IS NOT NULL;

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'person', value, 'addressee'
    FROM json_each(NEW.record_json, '$.addressee_person_ids')
    WHERE NEW.record_kind = 'observation';

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'person', value, 'participant'
    FROM json_each(NEW.record_json, '$.participant_ids')
    WHERE NEW.record_kind = 'event';

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'project', value, 'project'
    FROM json_each(NEW.record_json, '$.project_ids')
    WHERE NEW.record_kind IN ('event','goal');

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'person', value, 'participant'
    FROM json_each(NEW.record_json, '$.participant_person_ids')
    WHERE NEW.record_kind = 'project';

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'person',
           json_extract(NEW.record_json, '$.archive_owner_person_id'), 'relationship_owner'
    WHERE NEW.record_kind = 'relationship_snapshot';

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'person',
           json_extract(NEW.record_json, '$.other_person_id'), 'relationship_other'
    WHERE NEW.record_kind = 'relationship_snapshot';

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'project',
           json_extract(NEW.record_json, '$.scope.project_id'), 'scope'
    WHERE NEW.record_kind IN ('interpretation','interpretation_candidate')
      AND json_extract(NEW.record_json, '$.scope.kind') = 'project';

    INSERT OR IGNORE INTO personal_model_entity_memberships
        (generation_id, record_id, entity_kind, entity_id, role)
    SELECT NEW.generation_id, NEW.record_id, 'person', value, 'scope'
    FROM json_each(NEW.record_json, '$.scope.relationship_person_ids')
    WHERE NEW.record_kind IN ('interpretation','interpretation_candidate')
      AND json_extract(NEW.record_json, '$.scope.kind') = 'relationship';

    INSERT OR IGNORE INTO personal_model_entity_aliases
        (generation_id, entity_kind, entity_id, alias_kind, exact_value,
         normalized_value, source, source_account, verification, resolution_tier)
    SELECT NEW.generation_id, NEW.record_kind, NEW.record_id, 'canonical_id',
           NEW.record_id, lower(trim(NEW.record_id)), '', '', 'user_confirmed', 0
    WHERE NEW.record_kind IN ('person','project','goal');

    INSERT OR IGNORE INTO personal_model_entity_aliases
        (generation_id, entity_kind, entity_id, alias_kind, exact_value,
         normalized_value, source, source_account, verification, resolution_tier)
    SELECT NEW.generation_id, NEW.record_kind, NEW.record_id, 'canonical_key',
           json_extract(NEW.record_json, '$.canonical_key'),
           lower(trim(json_extract(NEW.record_json, '$.canonical_key'))),
           '', '', 'automatic', 0
    WHERE NEW.record_kind IN ('person','project','goal')
      AND json_extract(NEW.record_json, '$.canonical_key') IS NOT NULL;

    INSERT OR IGNORE INTO personal_model_entity_aliases
        (generation_id, entity_kind, entity_id, alias_kind, exact_value,
         normalized_value, source, source_account, verification, resolution_tier)
    SELECT NEW.generation_id, NEW.record_kind, NEW.record_id, 'display_name',
           COALESCE(json_extract(NEW.record_json, '$.display_name'),
                    json_extract(NEW.record_json, '$.title')),
           lower(trim(COALESCE(json_extract(NEW.record_json, '$.display_name'),
                               json_extract(NEW.record_json, '$.title')))),
           '', '', 'candidate', 2
    WHERE NEW.record_kind IN ('person','project','goal')
      AND COALESCE(json_extract(NEW.record_json, '$.display_name'),
                   json_extract(NEW.record_json, '$.title')) IS NOT NULL;

    INSERT OR IGNORE INTO personal_model_entity_aliases
        (generation_id, entity_kind, entity_id, alias_kind, exact_value,
         normalized_value, source, source_account, verification, resolution_tier)
    SELECT NEW.generation_id, NEW.record_kind, NEW.record_id,
           json_extract(alias.value, '$.kind'),
           json_extract(alias.value, '$.exact_value'),
           json_extract(alias.value, '$.normalized_value'),
           COALESCE(json_extract(alias.value, '$.source'), ''),
           COALESCE(json_extract(alias.value, '$.source_account'), ''),
           json_extract(alias.value, '$.verification'),
           CASE
             WHEN json_extract(alias.value, '$.kind') IN
                  ('username','email','phone','source_account_id')
                  AND json_extract(alias.value, '$.source') IS NOT NULL THEN 1
             WHEN json_extract(alias.value, '$.verification') = 'user_confirmed' THEN 1
             ELSE 2
           END
    FROM json_each(NEW.record_json, '$.aliases') AS alias
    WHERE NEW.record_kind IN ('person','project');
END;

-- Forward migration for generations staged before this projection existed.
INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT generation_id, record_id, record_kind, record_id, 'self'
FROM personal_model_records
WHERE record_kind IN ('person','project','goal','event');

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT generation_id, record_id, json_extract(record_json, '$.subject.kind'),
       json_extract(record_json, '$.subject.id'), 'subject'
FROM personal_model_records
WHERE record_kind IN ('fact','preference');

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT record.generation_id, record.record_id, 'person', member.value, 'participant'
FROM personal_model_records AS record,
     json_each(record.record_json, '$.participant_ids') AS member
WHERE record.record_kind = 'event';

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT record.generation_id, record.record_id, 'project', member.value, 'project'
FROM personal_model_records AS record,
     json_each(record.record_json, '$.project_ids') AS member
WHERE record.record_kind IN ('event','goal');

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT record.generation_id, record.record_id, 'person', member.value, 'participant'
FROM personal_model_records AS record,
     json_each(record.record_json, '$.participant_person_ids') AS member
WHERE record.record_kind = 'project';

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT generation_id, record_id, json_extract(record_json, '$.subject_kind'),
       json_extract(record_json, '$.subject_id'), 'subject'
FROM personal_model_records
WHERE record_kind = 'observation'
  AND json_extract(record_json, '$.subject_kind') IS NOT NULL
  AND json_extract(record_json, '$.subject_id') IS NOT NULL;

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT generation_id, record_id, 'person',
       json_extract(record_json, '$.speaker_person_id'), 'speaker'
FROM personal_model_records
WHERE record_kind = 'observation'
  AND json_extract(record_json, '$.speaker_person_id') IS NOT NULL;

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT record.generation_id, record.record_id, 'person', member.value, 'addressee'
FROM personal_model_records AS record,
     json_each(record.record_json, '$.addressee_person_ids') AS member
WHERE record.record_kind = 'observation';

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT generation_id, record_id, 'person',
       json_extract(record_json, '$.archive_owner_person_id'), 'relationship_owner'
FROM personal_model_records WHERE record_kind = 'relationship_snapshot';

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT generation_id, record_id, 'project',
       json_extract(record_json, '$.scope.project_id'), 'scope'
FROM personal_model_records
WHERE record_kind IN ('interpretation','interpretation_candidate')
  AND json_extract(record_json, '$.scope.kind') = 'project';

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT record.generation_id, record.record_id, 'person', member.value, 'scope'
FROM personal_model_records AS record,
     json_each(record.record_json, '$.scope.relationship_person_ids') AS member
WHERE record.record_kind IN ('interpretation','interpretation_candidate')
  AND json_extract(record.record_json, '$.scope.kind') = 'relationship';

INSERT OR IGNORE INTO personal_model_entity_memberships
    (generation_id, record_id, entity_kind, entity_id, role)
SELECT generation_id, record_id, 'person',
       json_extract(record_json, '$.other_person_id'), 'relationship_other'
FROM personal_model_records WHERE record_kind = 'relationship_snapshot';

INSERT OR IGNORE INTO personal_model_entity_aliases
    (generation_id, entity_kind, entity_id, alias_kind, exact_value,
     normalized_value, source, source_account, verification, resolution_tier)
SELECT generation_id, record_kind, record_id, 'canonical_id', record_id,
       lower(trim(record_id)), '', '', 'user_confirmed', 0
FROM personal_model_records WHERE record_kind IN ('person','project','goal');

INSERT OR IGNORE INTO personal_model_entity_aliases
    (generation_id, entity_kind, entity_id, alias_kind, exact_value,
     normalized_value, source, source_account, verification, resolution_tier)
SELECT generation_id, record_kind, record_id, 'canonical_key',
       json_extract(record_json, '$.canonical_key'),
       lower(trim(json_extract(record_json, '$.canonical_key'))),
       '', '', 'automatic', 0
FROM personal_model_records
WHERE record_kind IN ('person','project','goal')
  AND json_extract(record_json, '$.canonical_key') IS NOT NULL;

INSERT OR IGNORE INTO personal_model_entity_aliases
    (generation_id, entity_kind, entity_id, alias_kind, exact_value,
     normalized_value, source, source_account, verification, resolution_tier)
SELECT generation_id, record_kind, record_id, 'display_name',
       COALESCE(json_extract(record_json, '$.display_name'),
                json_extract(record_json, '$.title')),
       lower(trim(COALESCE(json_extract(record_json, '$.display_name'),
                           json_extract(record_json, '$.title')))),
       '', '', 'candidate', 2
FROM personal_model_records
WHERE record_kind IN ('person','project','goal')
  AND COALESCE(json_extract(record_json, '$.display_name'),
               json_extract(record_json, '$.title')) IS NOT NULL;

INSERT OR IGNORE INTO personal_model_entity_aliases
    (generation_id, entity_kind, entity_id, alias_kind, exact_value,
     normalized_value, source, source_account, verification, resolution_tier)
SELECT record.generation_id, record.record_kind, record.record_id,
       json_extract(alias.value, '$.kind'), json_extract(alias.value, '$.exact_value'),
       json_extract(alias.value, '$.normalized_value'),
       COALESCE(json_extract(alias.value, '$.source'), ''),
       COALESCE(json_extract(alias.value, '$.source_account'), ''),
       json_extract(alias.value, '$.verification'),
       CASE
         WHEN json_extract(alias.value, '$.kind') IN
              ('username','email','phone','source_account_id')
              AND json_extract(alias.value, '$.source') IS NOT NULL THEN 1
         WHEN json_extract(alias.value, '$.verification') = 'user_confirmed' THEN 1
         ELSE 2
       END
FROM personal_model_records AS record,
     json_each(record.record_json, '$.aliases') AS alias
WHERE record.record_kind IN ('person','project');

-- Query projections are part of the activated snapshot. Migration backfill is
-- intentionally above these guards; after installation only staged generations
-- may acquire or change projection rows.
CREATE TRIGGER IF NOT EXISTS personal_model_memberships_activated_no_insert
BEFORE INSERT ON personal_model_entity_memberships
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model memberships are immutable');
END;

CREATE TRIGGER IF NOT EXISTS personal_model_memberships_activated_no_update
BEFORE UPDATE ON personal_model_entity_memberships
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model memberships are immutable');
END;

CREATE TRIGGER IF NOT EXISTS personal_model_memberships_activated_no_delete
BEFORE DELETE ON personal_model_entity_memberships
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model memberships are immutable');
END;

CREATE TRIGGER IF NOT EXISTS personal_model_aliases_activated_no_insert
BEFORE INSERT ON personal_model_entity_aliases
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = NEW.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model aliases are immutable');
END;

CREATE TRIGGER IF NOT EXISTS personal_model_aliases_activated_no_update
BEFORE UPDATE ON personal_model_entity_aliases
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model aliases are immutable');
END;

CREATE TRIGGER IF NOT EXISTS personal_model_aliases_activated_no_delete
BEFORE DELETE ON personal_model_entity_aliases
WHEN EXISTS (
    SELECT 1 FROM personal_model_generations g
    WHERE g.id = OLD.generation_id AND g.state IN ('current','superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'activated personal-model aliases are immutable');
END;
