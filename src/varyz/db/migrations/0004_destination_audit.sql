-- Destination-result audit fields are a separate forward migration so
-- installations that already applied the context-platform schema upgrade
-- without losing their receipts.

ALTER TABLE context_requests ADD COLUMN conversation_id TEXT;

ALTER TABLE egress_executions ADD COLUMN adapter_version TEXT;
ALTER TABLE egress_executions
    ADD COLUMN approval_scope_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE consent_grants ADD COLUMN scope_valid_from TEXT;
ALTER TABLE consent_grants ADD COLUMN scope_valid_to TEXT;
