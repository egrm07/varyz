-- Deletion cascades across sections, so a deletion has to remember its words.
--
-- A conclusion's identity is a content fingerprint that includes its section.
-- That is right for regeneration -- the same sentence rebuilt from the same
-- evidence keeps its identity across builds -- and wrong for deletion: "I
-- rewrite pipelines by hand" struck as a value kept being shown as a goal,
-- because those are two fingerprints. The user did not reject a row; they
-- rejected a sentence about themselves.
--
-- Matching on text means the deletion must store the text. It cannot be read
-- back from `mirror_conclusions` at apply time, because the whole point is that
-- the row stops being written: one generation after the deletion, the sentence
-- exists nowhere except in the edit that removed it.
--
-- Backfilled from whatever the current Mirror still holds, so deletions made
-- before this migration cascade too. Any older deletion whose conclusion has
-- already been regenerated away keeps its previous fingerprint-only behaviour
-- rather than silently acquiring a new reach.

ALTER TABLE mirror_edits ADD COLUMN struck_text TEXT;

UPDATE mirror_edits
   SET struck_text = (
       SELECT c.title
         FROM mirror_conclusions c
         JOIN mirror_runs r ON r.id = c.run_id
        WHERE c.fingerprint = mirror_edits.fingerprint
        ORDER BY (r.state = 'current') DESC, r.generated_at DESC
        LIMIT 1
   )
 WHERE action = 'deleted' AND struck_text IS NULL;
