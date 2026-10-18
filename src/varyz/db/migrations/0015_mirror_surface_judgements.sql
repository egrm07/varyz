-- What a plan-shaped sentence *is*, when no pattern can tell.
--
-- The lexical veto in `mirror/goals.py` reads shapes: an imperative aimed at
-- somebody else, a weekday, a negation. Measured against 157 hand-labelled
-- surfaces it takes the goals section from 24% precision to 82%. What survives
-- is not a shape. "I decided to keep the thank you note in my essay" and "I
-- decided to make my own broker" are the same grammar with the same kind of
-- object; one is a step inside a task the person finished that afternoon, the
-- other is a direction they are still walking in. Every further rule tested
-- against those labels dropped one junk surface and one real goal, which is
-- fitting single cases rather than reading a shape.
--
-- So the question is asked of the local model, once per sentence, and the
-- answer is stored here. Two reasons it is a table and not a call:
--
-- Generation stays deterministic and offline. `varyz mirror generate` promises
-- no network and no model, and the same archive producing the same Mirror
-- twice is the reason a person can trust it. Judging happens in `varyz mirror
-- judge`, on the user's own initiative; generation only reads what is already
-- written down.
--
-- And a verdict is expensive. The surface is the key, so a sentence judged once
-- stays judged across regenerations, across graph rebuilds, and across the same
-- sentence appearing in two sections.

CREATE TABLE IF NOT EXISTS mirror_surface_judgements (
    surface_key    TEXT PRIMARY KEY,
    section        TEXT NOT NULL,
    -- The sentence as judged, kept so a reader can audit the verdict and so a
    -- changed prompt can be re-run without going back to the graph.
    surface        TEXT NOT NULL,
    -- 'direction', 'step', or 'unknown'. Only 'step' removes anything: a model
    -- that cannot answer must not be able to delete a person's goal.
    verdict        TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mirror_judgements_section
    ON mirror_surface_judgements (section, verdict);
