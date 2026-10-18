-- Cache one candidate-window reply, keyed by everything that was asked.
--
-- A candidate window is a live read of a stretch of the archive: 18 to 96
-- seconds each, 324 of them on compile-20260831-141645. Three attempts of that
-- run each read every window and then failed downstream in assembly, so about
-- 90 minutes of reading was bought and thrown away three times over. Nothing
-- about the reply depends on the attempt that asked for it, so a re-run after a
-- downstream failure should be able to pick the replies back up.
--
-- The key binds the prompt text, the request payload, the schema, the adapter
-- and the model, so a changed prompt or a changed model is a miss rather than a
-- stale hit. Unlike the annotation cache this is not generation-scoped and
-- carries no generation id: the same window read under the same inputs is the
-- same window whichever generation asked.
CREATE TABLE personal_model_candidate_window_cache (
    cache_key       TEXT PRIMARY KEY,
    adapter_name    TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    response_json   TEXT NOT NULL CHECK (json_valid(response_json)),
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_personal_model_candidate_window_cache_provenance
    ON personal_model_candidate_window_cache
       (adapter_name, adapter_version, model_name, prompt_version, schema_version);
