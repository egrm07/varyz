-- Agent gateway: principals, opaque credentials, least-privilege capabilities,
-- request attribution, and local-action idempotency. Protected payload bytes
-- remain in the existing receipt pipeline and are never stored here.

CREATE TABLE agent_principals (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    publisher       TEXT NOT NULL DEFAULT '',
    trust_state     TEXT NOT NULL DEFAULT 'active'
                    CHECK (trust_state IN ('active','revoked')),
    manifest_json   TEXT NOT NULL,
    manifest_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    revoked_at      TEXT
);
CREATE INDEX idx_agent_principals_state
    ON agent_principals (trust_state, created_at);

CREATE TABLE agent_tokens (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES agent_principals(id) ON DELETE CASCADE,
    label           TEXT NOT NULL DEFAULT 'default',
    token_hash      TEXT NOT NULL UNIQUE,
    valid_until     TEXT NOT NULL,
    revoked_at      TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT
);
CREATE INDEX idx_agent_tokens_agent
    ON agent_tokens (agent_id, revoked_at, valid_until);

CREATE TABLE agent_capabilities (
    id                  TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL REFERENCES agent_principals(id) ON DELETE CASCADE,
    effect_kind         TEXT NOT NULL CHECK (effect_kind IN ('context','action')),
    purpose             TEXT NOT NULL,
    resource_types_json TEXT NOT NULL DEFAULT '[]',
    tool_names_json     TEXT NOT NULL DEFAULT '[]',
    destinations_json   TEXT NOT NULL DEFAULT '[]',
    max_items           INTEGER NOT NULL CHECK (max_items >= 0),
    max_chars           INTEGER NOT NULL CHECK (max_chars >= 0),
    allow_background    INTEGER NOT NULL DEFAULT 0,
    valid_until         TEXT NOT NULL,
    revoked_at          TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_agent_capabilities_agent
    ON agent_capabilities (agent_id, effect_kind, purpose, revoked_at, valid_until);

CREATE TABLE agent_effects (
    request_id             TEXT PRIMARY KEY
                           REFERENCES context_requests(id) ON DELETE RESTRICT,
    prepared_id            TEXT NOT NULL UNIQUE,
    receipt_id             TEXT NOT NULL UNIQUE
                           REFERENCES egress_receipts(id) ON DELETE RESTRICT,
    agent_id               TEXT NOT NULL
                           REFERENCES agent_principals(id) ON DELETE RESTRICT,
    capability_id          TEXT NOT NULL
                           REFERENCES agent_capabilities(id) ON DELETE RESTRICT,
    effect_kind            TEXT NOT NULL CHECK (effect_kind IN ('context','action')),
    tool_name              TEXT,
    destination_chain_json TEXT NOT NULL,
    manifest_hash          TEXT NOT NULL,
    state                  TEXT NOT NULL CHECK (state IN
                              ('prepared','approved','denied','blocked',
                               'executed','expired')),
    expires_at             TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX idx_agent_effects_pending
    ON agent_effects (state, expires_at, created_at);
CREATE INDEX idx_agent_effects_agent
    ON agent_effects (agent_id, created_at);

CREATE TABLE gateway_action_keys (
    tool_name          TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL,
    external_action_id TEXT NOT NULL,
    response_json      TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (tool_name, idempotency_key)
);
