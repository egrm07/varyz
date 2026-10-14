-- Temporal Truth Graph, Private Context Kernel, durable jobs, and outbox.
-- Everything in this migration is additive. Entries and legacy facets remain
-- the canonical evidence and compatibility projection respectively.

CREATE TABLE internal_events (
    id              TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    aggregate_type  TEXT NOT NULL,
    aggregate_id    TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    consumed_at     TEXT,
    UNIQUE (event_type, aggregate_type, aggregate_id, payload_json)
);
CREATE INDEX idx_internal_events_pending
    ON internal_events (created_at) WHERE consumed_at IS NULL;

CREATE TABLE jobs (
    id                    TEXT PRIMARY KEY,
    job_key               TEXT NOT NULL UNIQUE,
    resource_type         TEXT NOT NULL,
    resource_id           TEXT NOT NULL,
    task                  TEXT NOT NULL,
    version               TEXT NOT NULL,
    state                 TEXT NOT NULL DEFAULT 'pending' CHECK (state IN
                              ('pending','leased','done','failed','superseded')),
    lease_owner           TEXT,
    lease_expires_at      TEXT,
    attempts              INTEGER NOT NULL DEFAULT 0,
    max_attempts          INTEGER NOT NULL DEFAULT 5,
    checkpoint_json       TEXT NOT NULL DEFAULT '{}',
    error_category        TEXT,
    error_detail          TEXT,
    next_retry_at         TEXT,
    input_content_hash    TEXT NOT NULL,
    output_generation_id  TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    completed_at          TEXT
);
CREATE INDEX idx_jobs_claim
    ON jobs (state, next_retry_at, lease_expires_at, created_at);
CREATE INDEX idx_jobs_resource ON jobs (resource_type, resource_id, task);

CREATE TABLE graph_runs (
    id                    TEXT PRIMARY KEY,
    generation_key        TEXT NOT NULL UNIQUE,
    extractor_name        TEXT NOT NULL,
    extractor_version     TEXT NOT NULL,
    model                 TEXT,
    prompt_version        TEXT,
    predicate_version     TEXT NOT NULL,
    identity_version      TEXT NOT NULL,
    input_content_hash    TEXT NOT NULL,
    state                 TEXT NOT NULL CHECK (state IN
                              ('staging','active','superseded','failed')),
    checkpoint_entry_id   TEXT,
    entries_total         INTEGER NOT NULL DEFAULT 0,
    entries_processed     INTEGER NOT NULL DEFAULT 0,
    error                 TEXT,
    started_at            TEXT NOT NULL,
    completed_at          TEXT,
    activated_at          TEXT
);
CREATE UNIQUE INDEX idx_graph_one_active
    ON graph_runs (state) WHERE state = 'active';
CREATE INDEX idx_graph_runs_state ON graph_runs (state, started_at);

CREATE TABLE predicate_registry (
    predicate       TEXT NOT NULL,
    version         TEXT NOT NULL,
    object_types    TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    inference_only  INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (predicate, version)
);

INSERT INTO predicate_registry
    (predicate, version, object_types, description, inference_only)
VALUES
    ('lived_in', '1', '["entity","location","text"]', 'Residence during an interval', 0),
    ('moved_to', '1', '["entity","location","text"]', 'A move or intended move', 0),
    ('worked_on', '1', '["entity","text"]', 'Work on a project or activity', 0),
    ('planned_to', '1', '["text","entity"]', 'An explicit plan', 0),
    ('decided_to', '1', '["text","entity"]', 'An explicit decision', 0),
    ('promised_to', '1', '["text","entity"]', 'An explicit promise', 0),
    ('requested', '1', '["text","entity"]', 'An explicit request', 0),
    ('felt_about', '1', '["text","entity"]', 'A subjective feeling', 0),
    ('believed', '1', '["text","entity","boolean"]', 'A stated belief', 0),
    ('attended', '1', '["entity","text"]', 'Attendance at an event', 0),
    ('occurred_on', '1', '["date","interval"]', 'Explicit event date or interval', 0),
    ('mentioned', '1', '["entity","text"]', 'A source mention', 0),
    ('caused_by', '1', '["entity","text"]', 'An inferred cause', 1),
    ('correlates_with', '1', '["entity","text"]', 'An inferred correlation', 1);

CREATE TABLE entities (
    id                  TEXT PRIMARY KEY,
    entity_type         TEXT NOT NULL CHECK (entity_type IN
                            ('person','place','organization','project','object','event')),
    canonical_label     TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                            ('active','merged','deleted')),
    merged_into_id      TEXT REFERENCES entities(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    graph_generation_id TEXT REFERENCES graph_runs(id) ON DELETE SET NULL,
    generation_version  TEXT NOT NULL
);
CREATE INDEX idx_entities_label ON entities (canonical_label);
CREATE INDEX idx_entities_type ON entities (entity_type, status);

CREATE TABLE entity_aliases (
    id                  TEXT PRIMARY KEY,
    entity_id           TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    namespace           TEXT NOT NULL,
    alias_value         TEXT NOT NULL,
    normalized_value    TEXT NOT NULL,
    source_entry_id     TEXT REFERENCES entries(id) ON DELETE SET NULL,
    confidence          REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification_state  TEXT NOT NULL CHECK (verification_state IN
                            ('verified','automatic','candidate','rejected')),
    graph_generation_id TEXT REFERENCES graph_runs(id) ON DELETE CASCADE,
    fingerprint         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (graph_generation_id, fingerprint)
);
CREATE INDEX idx_entity_alias_exact
    ON entity_aliases (namespace, normalized_value, verification_state);
CREATE INDEX idx_entity_alias_entity ON entity_aliases (entity_id);

CREATE TABLE entity_candidates (
    id                   TEXT PRIMARY KEY,
    left_entity_id       TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    right_entity_id      TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    reason_code          TEXT NOT NULL,
    score                REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    explanation          TEXT NOT NULL,
    state                TEXT NOT NULL DEFAULT 'pending' CHECK (state IN
                             ('pending','merged','separate','dismissed')),
    graph_generation_id  TEXT REFERENCES graph_runs(id) ON DELETE CASCADE,
    fingerprint          TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    reviewed_at          TEXT,
    UNIQUE (graph_generation_id, fingerprint),
    CHECK (left_entity_id <> right_entity_id)
);
CREATE INDEX idx_entity_candidates_pending
    ON entity_candidates (state, score DESC);

CREATE TABLE claims (
    id                    TEXT PRIMARY KEY,
    subject_entity_id     TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate             TEXT NOT NULL,
    predicate_version     TEXT NOT NULL,
    object_type           TEXT NOT NULL CHECK (object_type IN
                              ('entity','text','number','date','interval','location','boolean')),
    object_value_json     TEXT NOT NULL,
    object_surface        TEXT NOT NULL,
    object_entity_id      TEXT REFERENCES entities(id) ON DELETE SET NULL,
    polarity              TEXT NOT NULL CHECK (polarity IN ('positive','negative')),
    modality              TEXT NOT NULL CHECK (modality IN
                              ('fact','plan','belief','feeling','dream','report','request',
                               'promise','inference')),
    asserted_by_entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    observed_at           TEXT,
    observed_tz           TEXT,
    asserted_at           TEXT,
    valid_from            TEXT,
    valid_to              TEXT,
    temporal_precision    TEXT NOT NULL CHECK (temporal_precision IN
                              ('exact','day','month','year','decade','unknown')),
    temporal_inferred     INTEGER NOT NULL DEFAULT 0,
    original_time_text    TEXT,
    confidence            REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    sensitivity           TEXT NOT NULL CHECK (sensitivity IN
                              ('public','personal','sensitive','unclassified')),
    extractor_name        TEXT NOT NULL,
    extractor_model       TEXT,
    prompt_version        TEXT,
    graph_generation_id   TEXT NOT NULL REFERENCES graph_runs(id) ON DELETE CASCADE,
    fingerprint           TEXT NOT NULL,
    lifecycle_state       TEXT NOT NULL DEFAULT 'candidate' CHECK (lifecycle_state IN
                              ('candidate','active','superseded','rejected','withdrawn')),
    plan_state            TEXT CHECK (plan_state IN
                              ('planned','completed','abandoned','unknown')),
    origin                TEXT NOT NULL DEFAULT 'deterministic' CHECK (origin IN
                              ('deterministic','model','user')),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (graph_generation_id, fingerprint),
    FOREIGN KEY (predicate, predicate_version)
        REFERENCES predicate_registry(predicate, version),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
    CHECK (
      (object_type = 'entity' AND object_entity_id IS NOT NULL)
      OR (object_type <> 'entity' AND object_entity_id IS NULL)
    )
);
CREATE INDEX idx_claims_subject_predicate
    ON claims (subject_entity_id, predicate, lifecycle_state);
CREATE INDEX idx_claims_time ON claims (valid_from, valid_to);
CREATE INDEX idx_claims_generation
    ON claims (graph_generation_id, lifecycle_state);

CREATE TABLE claim_evidence (
    id                    TEXT PRIMARY KEY,
    claim_id              TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    entry_id              TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    chunk_id              INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    quote                 TEXT NOT NULL,
    speaker_entity_id     TEXT REFERENCES entities(id) ON DELETE SET NULL,
    evidence_role         TEXT NOT NULL CHECK (evidence_role IN
                              ('supports','contradicts','updates','mentions')),
    confidence            REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    char_start            INTEGER,
    char_end              INTEGER,
    sensitivity           TEXT NOT NULL CHECK (sensitivity IN
                              ('public','personal','sensitive','unclassified')),
    graph_generation_id   TEXT NOT NULL REFERENCES graph_runs(id) ON DELETE CASCADE,
    fingerprint           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (graph_generation_id, fingerprint),
    CHECK (length(quote) > 0),
    CHECK (char_start IS NULL OR char_start >= 0),
    CHECK (char_end IS NULL OR char_start IS NULL OR char_end >= char_start)
);
CREATE INDEX idx_claim_evidence_claim ON claim_evidence (claim_id);
CREATE INDEX idx_claim_evidence_entry ON claim_evidence (entry_id);

CREATE TRIGGER claim_evidence_last_deleted
AFTER DELETE ON claim_evidence
WHEN NOT EXISTS (SELECT 1 FROM claim_evidence WHERE claim_id = old.claim_id)
BEGIN
    UPDATE claims
       SET lifecycle_state = 'withdrawn', updated_at = CURRENT_TIMESTAMP
     WHERE id = old.claim_id AND origin <> 'user';
END;

CREATE TABLE claim_relations (
    id                    TEXT PRIMARY KEY,
    from_claim_id         TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    to_claim_id           TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    relation_type         TEXT NOT NULL CHECK (relation_type IN
                              ('supports','contradicts','supersedes','duplicates',
                               'narrows','broadens','caused_by','correlates_with')),
    confidence            REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    explanation           TEXT NOT NULL DEFAULT '',
    graph_generation_id   TEXT NOT NULL REFERENCES graph_runs(id) ON DELETE CASCADE,
    fingerprint           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (graph_generation_id, fingerprint),
    CHECK (from_claim_id <> to_claim_id)
);
CREATE INDEX idx_claim_relations_from ON claim_relations (from_claim_id, relation_type);
CREATE INDEX idx_claim_relations_to ON claim_relations (to_claim_id, relation_type);

CREATE TABLE predicate_quarantine (
    id                    TEXT PRIMARY KEY,
    proposed_predicate    TEXT NOT NULL,
    candidate_json        TEXT NOT NULL,
    entry_id              TEXT REFERENCES entries(id) ON DELETE CASCADE,
    graph_generation_id   TEXT NOT NULL REFERENCES graph_runs(id) ON DELETE CASCADE,
    reason_code           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (graph_generation_id, entry_id, proposed_predicate, candidate_json)
);

CREATE TABLE graph_overrides (
    id              TEXT PRIMARY KEY,
    target_type     TEXT NOT NULL CHECK (target_type IN
                      ('entity','alias','candidate','claim','evidence')),
    target_key      TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN
                      ('merge','split','accept','reject','change_label','correct_time',
                       'mark_speaker','change_sensitivity','user_assertion','keep_separate')),
    payload_json    TEXT NOT NULL DEFAULT '{}',
    actor           TEXT NOT NULL DEFAULT 'local_user',
    reason          TEXT,
    reverses_id     TEXT REFERENCES graph_overrides(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_graph_overrides_target
    ON graph_overrides (target_type, target_key, created_at);

CREATE TABLE policy_labels (
    id                  TEXT PRIMARY KEY,
    resource_type       TEXT NOT NULL,
    resource_id         TEXT NOT NULL,
    label               TEXT NOT NULL CHECK (label IN
                            ('public','personal','sensitive','unclassified')),
    label_source        TEXT NOT NULL CHECK (label_source IN
                            ('connector_default','deterministic_rule','inherited',
                             'local_classifier','user_override','destination_policy')),
    reasons_json        TEXT NOT NULL DEFAULT '[]',
    policy_version      TEXT NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_policy_labels_resource
    ON policy_labels (resource_type, resource_id, active, created_at);

CREATE TABLE policy_rules (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    precedence          TEXT NOT NULL CHECK (precedence IN
                            ('explicit_deny','user_resource_override','revoked_consent',
                             'destination','purpose','source_default','system_default')),
    priority            INTEGER NOT NULL DEFAULT 0,
    actor               TEXT,
    purpose             TEXT,
    destination         TEXT,
    resource_type       TEXT,
    resource_id         TEXT,
    source              TEXT,
    sensitivity         TEXT,
    effect              TEXT NOT NULL CHECK (effect IN
                            ('allow','deny','local_only','transform','ask','aggregate_only')),
    transformation      TEXT,
    reason_code         TEXT NOT NULL,
    version             TEXT NOT NULL,
    user_authored       INTEGER NOT NULL DEFAULT 0,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_policy_rules_active
    ON policy_rules (active, precedence, priority DESC);

CREATE TABLE consent_grants (
    id                  TEXT PRIMARY KEY,
    actor               TEXT NOT NULL,
    destination         TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    request_id          TEXT,
    conversation_id     TEXT,
    resource_type       TEXT,
    resource_id         TEXT,
    entity_id           TEXT,
    topic               TEXT,
    valid_from          TEXT,
    valid_until         TEXT,
    max_items           INTEGER,
    max_chars           INTEGER,
    used_items          INTEGER NOT NULL DEFAULT 0,
    used_chars          INTEGER NOT NULL DEFAULT 0,
    revoked_at          TEXT,
    created_at          TEXT NOT NULL,
    CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from)
);
CREATE INDEX idx_consent_scope
    ON consent_grants (actor, destination, purpose, revoked_at, valid_until);

CREATE TABLE context_requests (
    id                  TEXT PRIMARY KEY,
    actor               TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    destination         TEXT NOT NULL,
    intent_hash         TEXT NOT NULL,
    requested_types_json TEXT NOT NULL,
    requested_scope_json TEXT NOT NULL,
    max_items           INTEGER NOT NULL,
    max_chars           INTEGER NOT NULL,
    interaction         TEXT NOT NULL,
    user_present        INTEGER NOT NULL,
    reversible          INTEGER NOT NULL,
    consent_grant_id    TEXT REFERENCES consent_grants(id) ON DELETE SET NULL,
    policy_version      TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE context_decisions (
    id                  TEXT PRIMARY KEY,
    request_id          TEXT NOT NULL REFERENCES context_requests(id) ON DELETE CASCADE,
    item_id             TEXT NOT NULL,
    resource_type       TEXT NOT NULL,
    resource_id         TEXT NOT NULL,
    effect              TEXT NOT NULL CHECK (effect IN
                            ('allow','deny','local_only','transform','ask','aggregate_only')),
    reason_codes_json   TEXT NOT NULL,
    transformation      TEXT,
    input_hash          TEXT NOT NULL,
    output_hash         TEXT,
    policy_version      TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (request_id, item_id)
);

CREATE TABLE transformation_registry (
    name                TEXT NOT NULL,
    version             TEXT NOT NULL,
    local_only          INTEGER NOT NULL DEFAULT 1,
    deterministic       INTEGER NOT NULL DEFAULT 1,
    active              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (name, version)
);

INSERT INTO transformation_registry (name, version) VALUES
    ('select_claim', '1'),
    ('select_sentences', '1'),
    ('pseudonymize_people', '1'),
    ('generalize_location', '1'),
    ('generalize_date', '1'),
    ('redact_sensitive_attributes', '1'),
    ('aggregate_count', '1'),
    ('local_answer', '1');

CREATE TABLE destination_registry (
    destination         TEXT PRIMARY KEY,
    adapter_version     TEXT NOT NULL,
    destination_type    TEXT NOT NULL CHECK (destination_type IN
                            ('local','cloud_model','export','share','action',
                             'remote_sync','analytics','plugin')),
    supports_idempotency INTEGER NOT NULL DEFAULT 0,
    reversible_default  INTEGER NOT NULL DEFAULT 0,
    active              INTEGER NOT NULL DEFAULT 1
);

INSERT INTO destination_registry
    (destination, adapter_version, destination_type, supports_idempotency, reversible_default)
VALUES
    ('local', '1', 'local', 1, 1),
    ('cloud:claude', '1', 'cloud_model', 1, 0),
    ('cloud:openai', '1', 'cloud_model', 1, 0),
    ('cloud:deepseek', '1', 'cloud_model', 1, 0),
    ('cloud:fake', '1', 'cloud_model', 1, 0),
    ('export:file', '1', 'export', 1, 1),
    ('share:encrypted', '1', 'share', 1, 0);

CREATE TABLE egress_receipts (
    id                    TEXT PRIMARY KEY,
    request_id            TEXT NOT NULL REFERENCES context_requests(id) ON DELETE RESTRICT,
    payload_hash          TEXT NOT NULL,
    payload_item_count    INTEGER NOT NULL,
    payload_char_count    INTEGER NOT NULL,
    considered_ids_json   TEXT NOT NULL,
    decision_summary_json TEXT NOT NULL,
    policy_version        TEXT NOT NULL,
    transformation_versions_json TEXT NOT NULL,
    approval_required     INTEGER NOT NULL,
    consent_grant_id      TEXT REFERENCES consent_grants(id) ON DELETE SET NULL,
    receipt_version       TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (request_id, payload_hash)
);

CREATE TABLE egress_executions (
    id                    TEXT PRIMARY KEY,
    receipt_id            TEXT NOT NULL REFERENCES egress_receipts(id) ON DELETE RESTRICT,
    request_id            TEXT NOT NULL,
    payload_hash          TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    state                 TEXT NOT NULL CHECK (state IN
                              ('blocked','started','succeeded','failed','unknown')),
    result_category       TEXT NOT NULL,
    response_metadata_json TEXT NOT NULL DEFAULT '{}',
    external_action_id    TEXT,
    reversible            INTEGER NOT NULL,
    error                 TEXT,
    started_at            TEXT NOT NULL,
    completed_at          TEXT,
    UNIQUE (request_id, payload_hash, attempt)
);
CREATE INDEX idx_egress_execution_idempotency
    ON egress_executions (request_id, payload_hash, state);
