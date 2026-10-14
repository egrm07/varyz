"""Graph search that always expands selected claims back to source entries."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from varyz.db.dao import get_entry
from varyz.domain import SearchHit
from varyz.graph.models import Claim, EvidenceLink
from varyz.graph.repository import GraphRepository
from varyz.graph.temporal import parse_time_expression
from varyz.retrieval.planner import RetrievalMode, RetrievalPlan

TOKEN_RE = re.compile(r"[\w'-]{3,}", re.UNICODE)
STOP = {
    # Words that carry no subject matter. The list was fourteen words long and
    # held none of the commonest ones, which is half of why a question's own
    # grammar used to decide what came back.
    "about", "after", "again", "all", "also", "and", "any", "are", "because",
    "been", "before", "being", "between", "both", "but", "can", "cannot",
    "could", "did", "does", "doing", "done", "down", "during", "each", "even",
    "ever", "every", "for", "from", "further", "get", "got", "had", "has",
    "have", "having", "her", "here", "hers", "him", "his", "how", "however",
    "into", "its", "itself", "just", "let", "like", "made", "make", "many",
    "may", "might", "more", "most", "much", "must", "myself", "need", "never",
    "not", "now", "off", "once", "one", "only", "other", "our", "ours", "out",
    "over", "own", "put", "same", "shall", "she", "should", "since",
    "some", "still", "such", "than", "that", "the", "their", "theirs", "them",
    "then", "there", "these", "they", "this", "those", "through", "too",
    "under", "until", "upon", "use", "used", "very", "was", "way", "were",
    "what", "when", "where", "whether", "which", "while", "who", "whom",
    "whose", "why", "will", "with", "within", "without", "would", "yet",
    "you", "your", "yours",
}


# Words that decline to name a subject. "Who did I promise something to?" is
# not a question about a person called Someone; it is a question with the
# subject left open, and the archive's answer is every one of them.
UNSPECIFIC = {
    "anybody", "anyone", "anything", "everybody", "everyone", "everything",
    "people", "somebody", "someone", "something", "stuff", "things",
}

# What each predicate is called, and what its life is called. A predicate is a
# class of claim, and a question can be *about* the class rather than about any
# subject in it: "what have I promised and not delivered" names no person, no
# project and no topic -- it names the class and asks for its contents. The
# extractor writes `promised_to` and never writes the word "delivered", so
# without this the question matched the predicate and nothing else, and the
# guard below -- rightly, on its own terms -- threw the whole class away.
PREDICATE_VOCABULARY: dict[str, frozenset[str]] = {
    "promised_to": frozenset({
        "broke", "broken", "commit", "commitment", "commitments", "committed",
        "deliver", "delivered", "delivery", "due", "fulfil", "fulfill",
        "fulfilled", "keep", "kept", "outstanding", "overdue", "owe", "owed",
        "pending", "promise", "promised", "promises", "promising",
        "undelivered", "unfulfilled", "unkept",
    }),
    "decided_to": frozenset({
        "choice", "choices", "choose", "chose", "chosen", "decide", "decided",
        "decides", "deciding", "decision", "decisions", "settled",
    }),
    "planned_to": frozenset({
        "going", "intend", "intended", "intending", "intention", "intentions",
        "meant", "plan", "planned", "planning", "plans",
    }),
    "believed": frozenset({
        "belief", "beliefs", "believe", "believed", "opinion", "opinions",
        "think", "thinks", "thought", "value", "values", "view", "views",
    }),
    "felt_about": frozenset({
        "emotion", "emotions", "feel", "feeling", "feelings", "felt",
    }),
    "requested": frozenset({
        "ask", "asked", "asking", "asks", "request", "requested", "requests",
    }),
    "worked_on": frozenset({
        "project", "projects", "work", "worked", "working",
    }),
    "lived_in": frozenset({"lived", "living", "resided", "residence"}),
    "moved_to": frozenset({"move", "moved", "moving", "relocated", "relocation"}),
}


def _class_predicates(tokens: set[str]) -> frozenset[str]:
    """Predicates a question is asking for the whole of, if any.

    Every content word has to be the class's own vocabulary. One word from
    outside it -- "what did I decide about database indexing" -- names a
    subject, and a question that names a subject the archive has never heard of
    is answered by saying so, not by handing back every decision ever made.
    """

    content = {token for token in tokens if token not in UNSPECIFIC}
    if not content:
        return frozenset()
    return frozenset(
        predicate
        for predicate, vocabulary in PREDICATE_VOCABULARY.items()
        if content <= vocabulary
    )


def _tokens(text: str) -> set[str]:
    """Whole tokens, case-folded. Never substrings: `and` is inside Netherlands."""

    return {token.casefold() for token in TOKEN_RE.findall(text)}


def class_query_predicates(query: str, plan: RetrievalPlan) -> frozenset[str]:
    """Predicates this question asks for the whole of, for a caller to declare.

    An answer assembled this way is a different thing from an answer to the
    question as asked -- every promise, not the promises that match -- and the
    surface that returns it has to be able to say so.
    """

    if plan.mode == RetrievalMode.CONFLICT:
        return frozenset()
    return _class_predicates({token for token in _tokens(query) if token not in STOP})


@dataclass(frozen=True)
class GraphHit:
    claim: Claim
    evidence: tuple[EvidenceLink, ...]
    subject_label: str
    object_label: str | None
    score: float
    relations: tuple[str, ...] = ()
    restated_by: tuple[str, ...] = ()
    """Claims the graph calls duplicates of this one, folded in rather than listed.

    Saying the same thing twice is one thing said twice, and an answer with ten
    slots should spend them on ten different statements. Half the archive's
    promises are in a duplicate pair, so the list of what was promised came back
    padded with repeats of what it had already said. Their evidence comes with
    them: each occasion the thing was said is detail about it.
    """


class GraphQueryService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.repository = GraphRepository(conn)

    def search(
        self, query: str, plan: RetrievalPlan, *, limit: int = 20
    ) -> list[GraphHit]:
        run = self.repository.active_run()
        if run is None or plan.mode == RetrievalMode.ENTRY:
            return []
        tokens = {token for token in _tokens(query) if token not in STOP}
        rows = self.conn.execute(
            """SELECT c.*, s.canonical_label AS subject_label,
                      o.canonical_label AS object_label
               FROM claims c
               JOIN entities s ON s.id=c.subject_entity_id
               LEFT JOIN entities o ON o.id=c.object_entity_id
              WHERE c.graph_generation_id=?
                AND c.lifecycle_state IN ('active','superseded')
              ORDER BY c.confidence DESC,
                       COALESCE(c.valid_from,c.asserted_at,c.observed_at) DESC""",
            (run.id,),
        ).fetchall()
        time = parse_time_expression(query, None)
        classed = class_query_predicates(query, plan)
        scored: list[tuple[float, sqlite3.Row, float]] = []
        for row in rows:
            # What the claim is *about*, which is where relevance lives. The
            # canonical entity labels are the claim's subject matter; the
            # surface sentence is where it was said, and a question word landing
            # there is as often incidental as not -- "move" is inside "We should
            # probably move to America then" for a question about the
            # Netherlands. So a label match counts double a surface one.
            labels = _tokens(f"{row['subject_label'] or ''} {row['object_label'] or ''}")
            surface = _tokens(
                f"{row['object_surface'] or ''} {row['original_time_text'] or ''}"
            )
            matches = len(tokens & labels) + 0.5 * len(tokens & (surface - labels))
            # The predicate is a class, not a subject. The planner has already
            # routed on it, so counting it again admits every claim that shares
            # the question's verb -- 242 of them for one "decide" measured on
            # the real archive. It still admits a claim, because "When did I
            # move?" is a question whose only content word is the predicate; it
            # just no longer decides which of them comes back first.
            verb = row["predicate"].replace("_", " ")
            predicate_matches = sum(1 for token in tokens if token in verb)
            # Confidence is how sure the extractor was, not how relevant the
            # claim is. As the leading term it ranked the archive's most certain
            # claims first for every question ever asked.
            score = matches + predicate_matches * 0.05 + float(row["confidence"]) * 0.2
            if time.value:
                target = time.value.isoformat()
                lower = row["valid_from"] or "0000"
                upper = row["valid_to"] or "9999"
                if lower <= target <= upper:
                    score += 1.0
                elif row["asserted_at"] and row["asserted_at"].startswith(target[:10]):
                    score += 0.8
            if plan.mode == RetrievalMode.CONFLICT:
                conflicts = self.conn.execute(
                    """SELECT COUNT(*) FROM claim_relations
                       WHERE relation_type='contradicts'
                         AND (from_claim_id=? OR to_claim_id=?)""",
                    (row["id"], row["id"]),
                ).fetchone()[0]
                if not conflicts:
                    continue
                score += conflicts
            if plan.mode == RetrievalMode.ENTITY_NEIGHBORHOOD and matches:
                score += 0.5
            if (
                matches
                or predicate_matches
                or time.value
                or not tokens
                or row["predicate"] in classed
            ):
                scored.append((score, row, matches))
        # A claim admitted only by the question's verb is noise as soon as
        # anything actually matched the subject matter, and when nothing did,
        # words the archive has never seen are the answer: it does not know.
        # Not in conflict mode: there the surviving claims are already only the
        # ones that contradict something, and "what contradicts" is vocabulary
        # about the question, not about the subject, so it matches no claim.
        # One incidental word is not a subject. A claim has to reach either a
        # canonical entity the question named or two of its words to count as
        # being about it -- and never more than that, however long the question.
        floor = min(0.4 * len(tokens), 1.0)
        on_subject = [item for item in scored if item[2] >= floor]
        if classed:
            # Before the subject-matter floor, not after it. A question of this
            # shape names no subject, so a claim whose surface happens to hold
            # two of its words has not matched one either -- and one such
            # coincidence used to outrank all twenty-nine promises.
            #
            # The question is about the class itself, so the class is the
            # answer and recency decides the order: the promise made last week
            # is the one still owed, and the one made in 2019 is history.
            scored = [item for item in scored if item[1]["predicate"] in classed]
            scored.sort(
                key=lambda item: (
                    item[1]["valid_from"]
                    or item[1]["asserted_at"]
                    or item[1]["observed_at"]
                    or "",
                    item[1]["id"],
                )
            )
            scored = [(float(index), row, match) for index, (_, row, match) in enumerate(scored)]
        elif on_subject:
            scored = on_subject
        elif (
            plan.mode != RetrievalMode.CONFLICT
            and tokens
            and not all(
                any(token in row["predicate"] for _, row, _ in scored) for token in tokens
            )
        ):
            scored = []
        scored.sort(key=lambda value: (value[0], value[1]["id"]), reverse=True)
        # Fold restatements into the claim they restate before the limit is
        # applied, so the slots go to distinct statements. The ranking already
        # decided which of a duplicate pair speaks for it; this only stops the
        # others from each taking a slot to say it again.
        duplicates = self._duplicates(run.id)
        folded: dict[str, list[str]] = {}
        spoken_for: dict[str, str] = {}
        distinct: list[tuple[float, sqlite3.Row, float]] = []
        for item in scored:
            claim_id = str(item[1]["id"])
            representative = spoken_for.get(claim_id)
            if representative is not None:
                folded[representative].append(claim_id)
                continue
            folded[claim_id] = []
            distinct.append(item)
            # The whole chain, not just the direct partners: where A restates B
            # and B restates C, all three are one statement even though no edge
            # joins A to C.
            pending = [claim_id]
            while pending:
                for other in duplicates.get(pending.pop(), ()):
                    if other != claim_id and other not in spoken_for:
                        spoken_for[other] = claim_id
                        pending.append(other)

        hits: list[GraphHit] = []
        for score, row, _ in distinct[:limit]:
            restated_by = tuple(folded.get(str(row["id"]), ()))
            evidence = tuple(self.repository.evidence_for_claim(row["id"]))
            # Each restatement is another occasion the thing was said, and the
            # quote behind it is evidence for the same claim.
            for other in restated_by:
                evidence += tuple(self.repository.evidence_for_claim(other))
            if not evidence and row["origin"] != "user":
                continue
            claim = self.repository.get_claim(row["id"])
            if claim is None:
                continue
            relations = tuple(
                relation["relation_type"]
                for relation in self.conn.execute(
                    """SELECT relation_type FROM claim_relations
                       WHERE from_claim_id=? OR to_claim_id=?
                       ORDER BY relation_type""",
                    (row["id"], row["id"]),
                )
            )
            hits.append(
                GraphHit(
                    claim=claim,
                    evidence=evidence,
                    subject_label=row["subject_label"],
                    object_label=row["object_label"],
                    score=score,
                    relations=relations,
                    restated_by=restated_by,
                )
            )
        return hits

    def _duplicates(self, generation_id: str) -> dict[str, tuple[str, ...]]:
        """Which claims of this generation the graph calls duplicates of which.

        Both directions, because the relation is symmetric in meaning and stored
        once with an arbitrary side as `from`. One read: the table holds a few
        hundred rows for a graph holding a thousand claims.
        """

        pairs: dict[str, list[str]] = {}
        for row in self.conn.execute(
            """SELECT from_claim_id, to_claim_id FROM claim_relations
               WHERE graph_generation_id=? AND relation_type='duplicates'""",
            (generation_id,),
        ):
            left, right = str(row["from_claim_id"]), str(row["to_claim_id"])
            pairs.setdefault(left, []).append(right)
            pairs.setdefault(right, []).append(left)
        return {key: tuple(value) for key, value in pairs.items()}

    def expand_to_entries(self, hits: list[GraphHit], *, limit: int = 20) -> list[SearchHit]:
        expanded: list[SearchHit] = []
        seen: set[str] = set()
        for graph_hit in hits:
            for evidence in graph_hit.evidence:
                if evidence.entry_id in seen:
                    continue
                entry = get_entry(self.conn, evidence.entry_id)
                if entry is None:
                    continue
                seen.add(entry.id)
                relation_note = (
                    f" [graph relations: {', '.join(graph_hit.relations)}]"
                    if graph_hit.relations
                    else ""
                )
                expanded.append(
                    SearchHit(
                        entry=entry,
                        score=graph_hit.score,
                        chunk=evidence.quote + relation_note,
                    )
                )
                if len(expanded) >= limit:
                    return expanded
        return expanded


def merge_entry_hits(
    graph_hits: list[SearchHit], entry_hits: list[SearchHit], *, limit: int
) -> list[SearchHit]:
    by_id: dict[str, SearchHit] = {}
    for hit in graph_hits + entry_hits:
        previous = by_id.get(hit.entry.id)
        if previous is None or hit.score > previous.score:
            by_id[hit.entry.id] = hit
    return sorted(by_id.values(), key=lambda hit: hit.score, reverse=True)[:limit]
