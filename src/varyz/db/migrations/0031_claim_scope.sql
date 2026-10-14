-- What a claim is about: the life around the work, or the work itself.
--
-- The semantic extractor already decided this for every facet it read and then
-- threw the work ones away, on the ground that a build ticket is not a value.
-- That is true of the mirror, which is the surface that was being protected,
-- and false of every other one: 1,588 working sessions in this archive
-- produced 25 claims, and the questions their owner actually asked -- where a
-- project was left, why a dependency was dropped -- were the ones the graph
-- could not answer. The judgement is kept instead of acted on here, and each
-- surface applies it for itself.
--
-- NULL means a claim written before this column existed, or one no facet
-- scoped. It is read as 'life', which is what every claim already in the table
-- was.
ALTER TABLE claims ADD COLUMN scope TEXT;

CREATE INDEX IF NOT EXISTS idx_claims_scope
    ON claims(graph_generation_id, scope, predicate);
