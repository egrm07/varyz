-- Query shapes exercised by the 100,000-entry context-platform baseline.
-- These indexes are additive and preserve every existing row.

CREATE INDEX idx_entity_alias_source_identity
    ON entity_aliases
       (source_entry_id, namespace, normalized_value, created_at);

CREATE INDEX idx_entity_alias_verified_identity
    ON entity_aliases
       (namespace, normalized_value, verification_state, created_at);
