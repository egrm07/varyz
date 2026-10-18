"""Transactional SQLite persistence for versioned graph generations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from itertools import combinations

from varyz.domain import Entry, Sensitivity, iso, new_id, utc_now
from varyz.events import emit_event
from varyz.graph.identity import ConservativeIdentityResolver
from varyz.graph.models import (
    Claim,
    ClaimRelationType,
    Entity,
    EvidenceLink,
    ExtractionBatch,
    GraphRun,
)
from varyz.graph.temporal import claim_relation
from varyz.privacy.labels import label_resource
from varyz.privacy.models import LabelSource

MIN_REPLACEMENT_COVERAGE = 0.90


@dataclass(frozen=True)
class RebuildResult:
    run_id: str | None
    generation_key: str
    entries_total: int
    entries_processed: int
    claims: int
    aliases: int
    quarantined: int
    # Entries staged from a previous generation's extraction rather than
    # extracted again. The gap between this and `entries_processed` is what a
    # rebuild actually paid for.
    entries_reused: int = 0
    # Entries the extractor could not answer twice in a row. They are staged
    # quarantined and the run continues; a generation is not worth discarding
    # over one slow prompt.
    entries_failed: int = 0
    no_op: bool = False
    dry_run: bool = False
    stopped: bool = False


def _worth_staging(batch: ExtractionBatch) -> list[dict]:
    """Aliases with a reader, in the order the extractor produced them.

    Every extractor emits `system:self` for every entry it looks at, whether or
    not anything in that entry turned out to be about the archive's owner. That
    is one alias row, one entity policy label and one alias policy label per
    entry per generation — 45,858 of each here, to support 696 claims — and it
    is what made the owner's entity own 290,968 of 485,251 alias rows.

    It is dropped when no claim in the batch names it, because `_stage_claim`
    reads exactly three keys off a claim and nothing else consults the row.
    Every other alias is kept even when no claim references it: an alias row
    saying this entry came from a person is how the Mirror counts who was
    present, and that is evidence in its own right.
    """
    referenced = {
        key
        for claim in batch.claims
        for key in (claim.subject_key, claim.object_entity_key, claim.asserted_by_key)
        if key
    }
    return [
        alias
        for alias in batch.aliases
        if str(alias.get("namespace")) != "system"
        or str(alias.get("value")) != "self"
        or str(alias["key"]) in referenced
    ]


def _version_key(value: str | None) -> tuple[int, ...] | tuple[()]:
    """Order extractor versions numerically; unparsable ones never outrank."""
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts)


def _row_to_run(row: sqlite3.Row) -> GraphRun:
    return GraphRun(
        id=row["id"],
        generation_key=row["generation_key"],
        extractor_name=row["extractor_name"],
        extractor_version=row["extractor_version"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        predicate_version=row["predicate_version"],
        identity_version=row["identity_version"],
        input_content_hash=row["input_content_hash"],
        state=row["state"],
        entries_total=row["entries_total"],
        entries_processed=row["entries_processed"],
        checkpoint_entry_id=row["checkpoint_entry_id"],
        error=row["error"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        activated_at=row["activated_at"],
    )


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        id=row["id"],
        subject_entity_id=row["subject_entity_id"],
        predicate=row["predicate"],
        predicate_version=row["predicate_version"],
        object_type=row["object_type"],
        object_value=json.loads(row["object_value_json"]),
        object_surface=row["object_surface"],
        object_entity_id=row["object_entity_id"],
        polarity=row["polarity"],
        modality=row["modality"],
        asserted_by_entity_id=row["asserted_by_entity_id"],
        observed_at=row["observed_at"],
        observed_tz=row["observed_tz"],
        asserted_at=row["asserted_at"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        temporal_precision=row["temporal_precision"],
        temporal_inferred=bool(row["temporal_inferred"]),
        original_time_text=row["original_time_text"],
        confidence=row["confidence"],
        sensitivity=row["sensitivity"],
        extractor_name=row["extractor_name"],
        extractor_model=row["extractor_model"],
        prompt_version=row["prompt_version"],
        fingerprint=row["fingerprint"],
        lifecycle_state=row["lifecycle_state"],
        plan_state=row["plan_state"],
        scope=row["scope"],
        origin=row["origin"],
        graph_generation_id=row["graph_generation_id"],
    )


class GraphRepository:
    predicate_version = "1"
    identity_version = "1"

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        predicate_version: str = "1",
        identity_version: str = "1",
        candidate_threshold: float = 0.55,
    ):
        self.conn = conn
        self.predicate_version = predicate_version
        self.identity_version = identity_version
        self.candidate_threshold = candidate_threshold

    def active_run(self) -> GraphRun | None:
        row = self.conn.execute(
            "SELECT * FROM graph_runs WHERE state='active'"
        ).fetchone()
        return _row_to_run(row) if row else None

    def begin_run(
        self,
        *,
        generation_key: str,
        extractor_name: str,
        extractor_version: str,
        model: str | None,
        prompt_version: str | None,
        input_content_hash: str,
        entries_total: int,
    ) -> tuple[GraphRun, bool]:
        existing = self.conn.execute(
            "SELECT * FROM graph_runs WHERE generation_key=?", (generation_key,)
        ).fetchone()
        if existing and existing["state"] == "active":
            return _row_to_run(existing), True
        if existing:
            with self.conn:
                self.conn.execute(
                    """UPDATE graph_runs SET state='staging', error=NULL,
                       completed_at=NULL, activated_at=NULL WHERE id=?""",
                    (existing["id"],),
                )
            resumed = _row_to_run(
                self.conn.execute(
                    "SELECT * FROM graph_runs WHERE id=?", (existing["id"],)
                ).fetchone()
            )
            self._seed_owner_identity(resumed)
            return resumed, False

        run_id = new_id()
        with self.conn:
            self.conn.execute(
                """INSERT INTO graph_runs
                   (id, generation_key, extractor_name, extractor_version, model,
                    prompt_version, predicate_version, identity_version,
                    input_content_hash, state, entries_total, started_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'staging',?,?)""",
                (
                    run_id,
                    generation_key,
                    extractor_name,
                    extractor_version,
                    model,
                    prompt_version,
                    self.predicate_version,
                    self.identity_version,
                    input_content_hash,
                    entries_total,
                    iso(utc_now()),
                ),
            )
        run = _row_to_run(
            self.conn.execute("SELECT * FROM graph_runs WHERE id=?", (run_id,)).fetchone()
        )
        self._seed_owner_identity(run)
        return run, False

    def _seed_owner_identity(self, run: GraphRun) -> None:
        """Give every generation exactly one `system:self` alias, up front.

        Staging used to write one per entry, so the row was guaranteed by sheer
        repetition. Now that unreferenced ones are dropped, an archive whose
        entries happen to make no first-person claim would have no owner
        identity at all, and `self_entity_id` — which the whole Mirror hangs
        off — would return None. Seeding it costs one row per generation and
        makes the guarantee explicit instead of incidental.
        """
        resolver = ConservativeIdentityResolver(
            self.conn,
            run.id,
            run.extractor_version,
            candidate_threshold=self.candidate_threshold,
        )
        with self.conn:
            resolution = resolver.resolve(
                {
                    "key": "self",
                    "entity_type": "person",
                    "label": "Me",
                    "namespace": "system",
                    "value": "self",
                    "source_entry_id": None,
                    "verification_state": "verified",
                    "confidence": 1.0,
                }
            )
            label_resource(
                self.conn,
                "entity",
                resolution.entity_id,
                Sensitivity.PERSONAL,
                LabelSource.INHERITED,
                [f"graph_generation:{run.id}"],
            )
            # And the alias row it just wrote. stage_batch labels aliases by
            # looking them up by source entry, and this one has no source
            # entry, so it was the one active alias in the archive with no
            # policy label -- which is `privacy doctor` reporting an error on
            # every graph anyone builds.
            for alias_row in self.conn.execute(
                """SELECT id FROM entity_aliases
                   WHERE entity_id=? AND graph_generation_id=?
                     AND source_entry_id IS NULL""",
                (resolution.entity_id, run.id),
            ).fetchall():
                label_resource(
                    self.conn,
                    "alias",
                    alias_row["id"],
                    Sensitivity.PERSONAL,
                    LabelSource.INHERITED,
                    [f"graph_generation:{run.id}"],
                )

    def stage_batch(self, run_id: str, entry: Entry, batch: ExtractionBatch) -> None:
        run_row = self.conn.execute(
            "SELECT * FROM graph_runs WHERE id=?", (run_id,)
        ).fetchone()
        if run_row is None or run_row["state"] != "staging":
            raise RuntimeError("graph run is not staging")
        if (
            run_row["checkpoint_entry_id"]
            and entry.id <= run_row["checkpoint_entry_id"]
        ):
            return

        with self.conn:
            resolver = ConservativeIdentityResolver(
                self.conn,
                run_id,
                run_row["extractor_version"],
                candidate_threshold=self.candidate_threshold,
            )
            entity_by_key: dict[str, str] = {}
            for alias in _worth_staging(batch):
                resolution = resolver.resolve(alias)
                entity_by_key[str(alias["key"])] = resolution.entity_id
                label_resource(
                    self.conn,
                    "entity",
                    resolution.entity_id,
                    entry.sensitivity,
                    LabelSource.INHERITED,
                    [f"entry:{entry.id}"],
                )
                alias_rows = self.conn.execute(
                    """SELECT id FROM entity_aliases
                       WHERE entity_id=? AND graph_generation_id=?
                         AND source_entry_id=?""",
                    (resolution.entity_id, run_id, entry.id),
                ).fetchall()
                for alias_row in alias_rows:
                    label_resource(
                        self.conn,
                        "alias",
                        alias_row["id"],
                        entry.sensitivity,
                        LabelSource.INHERITED,
                        [f"entry:{entry.id}"],
                    )

            for raw in batch.quarantined:
                self._quarantine(
                    run_id,
                    entry.id,
                    str(raw.get("predicate", "unknown")),
                    raw,
                    str(raw.get("reason_code", "extractor_quarantine")),
                )
            for candidate in batch.claims:
                self._stage_claim(run_id, entry, candidate, entity_by_key)

            self.conn.execute(
                """UPDATE graph_runs SET checkpoint_entry_id=?,
                   entries_processed=entries_processed+1 WHERE id=?""",
                (entry.id, run_id),
            )
            task_version = f"{run_row['extractor_name']}:{run_row['extractor_version']}"
            self.conn.execute(
                """UPDATE jobs SET state='done', output_generation_id=?,
                   completed_at=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL
                   WHERE resource_type='entry' AND resource_id=?
                     AND task='graph_extract' AND version=?
                     AND input_content_hash=?""",
                (
                    run_id,
                    iso(utc_now()),
                    iso(utc_now()),
                    entry.id,
                    task_version,
                    entry.content_hash,
                ),
            )

    def _stage_claim(
        self,
        run_id: str,
        entry: Entry,
        candidate,
        entity_by_key: dict[str, str],
    ) -> str | None:
        supported = self.conn.execute(
            """SELECT object_types, inference_only FROM predicate_registry
               WHERE predicate=? AND version=? AND active=1""",
            (candidate.predicate, candidate.predicate_version),
        ).fetchone()
        if supported is None:
            self._quarantine(
                run_id,
                entry.id,
                candidate.predicate,
                candidate.model_dump(mode="json"),
                "unknown_predicate",
            )
            return None
        if candidate.object_type not in json.loads(supported["object_types"]):
            self._quarantine(
                run_id,
                entry.id,
                candidate.predicate,
                candidate.model_dump(mode="json"),
                "unsupported_object_type",
            )
            return None
        if supported["inference_only"] and candidate.modality != "inference":
            self._quarantine(
                run_id,
                entry.id,
                candidate.predicate,
                candidate.model_dump(mode="json"),
                "inference_predicate_requires_inference_modality",
            )
            return None

        subject_id = entity_by_key.get(candidate.subject_key)
        object_entity_id = (
            entity_by_key.get(candidate.object_entity_key)
            if candidate.object_entity_key
            else None
        )
        asserted_by_id = (
            entity_by_key.get(candidate.asserted_by_key)
            if candidate.asserted_by_key
            else None
        )
        if subject_id is None or (
            candidate.object_type == "entity" and object_entity_id is None
        ):
            self._quarantine(
                run_id,
                entry.id,
                candidate.predicate,
                candidate.model_dump(mode="json"),
                "unresolved_entity_key",
            )
            return None

        evidence_entries: dict[str, sqlite3.Row] = {}
        for evidence in candidate.evidence:
            evidence_entry = self.conn.execute(
                "SELECT id, body, sensitivity FROM entries WHERE id=?",
                (evidence.entry_id,),
            ).fetchone()
            if evidence_entry is None or evidence.quote not in evidence_entry["body"]:
                self._quarantine(
                    run_id,
                    entry.id,
                    candidate.predicate,
                    candidate.model_dump(mode="json"),
                    "invalid_evidence_quote",
                )
                return None
            evidence_entries[evidence.entry_id] = evidence_entry
            if evidence.char_start is not None:
                actual = evidence_entry["body"][
                    evidence.char_start : evidence.char_end
                ]
                if actual != evidence.quote:
                    self._quarantine(
                        run_id,
                        entry.id,
                        candidate.predicate,
                        candidate.model_dump(mode="json"),
                        "invalid_evidence_offsets",
                    )
                    return None

        fingerprint = candidate.fingerprint(subject_id, object_entity_id)
        existing = self.conn.execute(
            """SELECT id FROM claims
               WHERE graph_generation_id=? AND fingerprint=?""",
            (run_id, fingerprint),
        ).fetchone()
        if existing:
            return existing["id"]

        claim_id = new_id()
        now = iso(utc_now())
        run_meta = self.conn.execute(
            """SELECT extractor_name, model, prompt_version FROM graph_runs WHERE id=?""",
            (run_id,),
        ).fetchone()
        values = (
            claim_id,
            subject_id,
            candidate.predicate,
            candidate.predicate_version,
            candidate.object_type,
            json.dumps(candidate.object_value, ensure_ascii=False, sort_keys=True),
            candidate.object_surface,
            object_entity_id,
            candidate.polarity,
            candidate.modality,
            asserted_by_id,
            iso(candidate.observed_at),
            candidate.observed_tz,
            iso(candidate.asserted_at),
            iso(candidate.valid_from),
            iso(candidate.valid_to),
            candidate.temporal_precision,
            int(candidate.temporal_inferred),
            candidate.original_time_text,
            candidate.confidence,
            candidate.sensitivity,
            run_meta["extractor_name"],
            run_meta["model"],
            run_meta["prompt_version"],
            run_id,
            fingerprint,
            "candidate",
            candidate.plan_state,
            candidate.scope,
            candidate.origin,
            now,
            now,
        )
        self.conn.execute(
            f"""INSERT INTO claims
                (id, subject_entity_id, predicate, predicate_version, object_type,
                 object_value_json, object_surface, object_entity_id, polarity, modality,
                 asserted_by_entity_id, observed_at, observed_tz, asserted_at,
                 valid_from, valid_to, temporal_precision, temporal_inferred,
                 original_time_text, confidence, sensitivity, extractor_name,
                 extractor_model, prompt_version, graph_generation_id, fingerprint,
                 lifecycle_state, plan_state, scope, origin, created_at, updated_at)
                VALUES ({','.join('?' for _ in values)})""",
            values,
        )
        for evidence in candidate.evidence:
            evidence_fingerprint = hashlib.sha256(
                f"{fingerprint}\0{evidence.entry_id}\0{evidence.chunk_id}\0"
                f"{evidence.evidence_role}\0{evidence.quote}".encode()
            ).hexdigest()
            self.conn.execute(
                """INSERT OR IGNORE INTO claim_evidence
                   (id, claim_id, entry_id, chunk_id, quote, speaker_entity_id,
                    evidence_role, confidence, char_start, char_end, sensitivity,
                    graph_generation_id, fingerprint, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id(),
                    claim_id,
                    evidence.entry_id,
                    evidence.chunk_id,
                    evidence.quote,
                    entity_by_key.get(evidence.speaker_key)
                    if evidence.speaker_key
                    else None,
                    evidence.evidence_role,
                    evidence.confidence,
                    evidence.char_start,
                    evidence.char_end,
                    evidence_entries[evidence.entry_id]["sensitivity"],
                    run_id,
                    evidence_fingerprint,
                    now,
                ),
            )
        label_resource(
            self.conn,
            "claim",
            claim_id,
            candidate.sensitivity,
            LabelSource.INHERITED,
            [f"entry:{entry_id}" for entry_id in evidence_entries],
        )
        evidence_rows = self.conn.execute(
            "SELECT id, entry_id, sensitivity FROM claim_evidence WHERE claim_id=?",
            (claim_id,),
        ).fetchall()
        for evidence_row in evidence_rows:
            label_resource(
                self.conn,
                "evidence_quote",
                evidence_row["id"],
                evidence_row["sensitivity"],
                LabelSource.INHERITED,
                [f"claim:{claim_id}", f"entry:{evidence_row['entry_id']}"],
            )
        return claim_id

    def _quarantine(
        self,
        run_id: str,
        entry_id: str,
        predicate: str,
        candidate: dict,
        reason: str,
    ) -> None:
        material = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str)
        self.conn.execute(
            """INSERT OR IGNORE INTO predicate_quarantine
               (id, proposed_predicate, candidate_json, entry_id,
                graph_generation_id, reason_code, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                new_id(),
                predicate,
                material,
                entry_id,
                run_id,
                reason,
                iso(utc_now()),
            ),
        )

    def activate(self, run_id: str, *, allow_downgrade: bool = False) -> None:
        with self.conn:
            run = self.conn.execute(
                "SELECT * FROM graph_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or run["state"] != "staging":
                raise RuntimeError("graph run is not staging")
            active = self.conn.execute(
                "SELECT * FROM graph_runs WHERE state='active'"
            ).fetchone()
            # A daemon that has been up for days is running the code it started
            # with. Without this it happily supersedes a graph built by a newer
            # extractor with its own older output.
            if (
                not allow_downgrade
                and active is not None
                and _version_key(active["extractor_version"])
                > _version_key(run["extractor_version"])
            ):
                raise RuntimeError(
                    f"refusing to supersede extractor version "
                    f"{active['extractor_version']} with {run['extractor_version']}; "
                    f"rebuild with an explicit --version to override"
                )
            if (
                not allow_downgrade
                and active is not None
                and active["input_content_hash"] == run["input_content_hash"]
            ):
                old_claims, old_evidence = self._coverage_counts(active["id"])
                new_claims, new_evidence = self._coverage_counts(run_id)
                losses = []
                if old_claims and new_claims < old_claims * MIN_REPLACEMENT_COVERAGE:
                    losses.append(f"claims {new_claims}/{old_claims}")
                if old_evidence and new_evidence < old_evidence * MIN_REPLACEMENT_COVERAGE:
                    losses.append(f"evidence {new_evidence}/{old_evidence}")
                if losses:
                    raise RuntimeError(
                        "refusing to activate a graph that loses more than "
                        f"{1 - MIN_REPLACEMENT_COVERAGE:.0%} of same-input coverage: "
                        + ", ".join(losses)
                        + "; pass the explicit downgrade override only after review"
                    )
            unsupported = self.conn.execute(
                """SELECT COUNT(*) FROM claims c
                   WHERE c.graph_generation_id=? AND c.origin<>'user'
                     AND NOT EXISTS (
                       SELECT 1 FROM claim_evidence e WHERE e.claim_id=c.id
                     )""",
                (run_id,),
            ).fetchone()[0]
            if unsupported:
                raise RuntimeError(f"{unsupported} derived claims have no evidence")

            self._detect_relations(run_id)
            from varyz.graph.overrides import apply_overrides

            apply_overrides(self.conn, run_id)
            self.conn.execute(
                """UPDATE claims SET lifecycle_state='active', updated_at=?
                   WHERE graph_generation_id=? AND lifecycle_state='candidate'""",
                (iso(utc_now()), run_id),
            )
            self.conn.execute(
                """UPDATE entities SET status='deleted', updated_at=?
                   WHERE status='active'
                     AND id NOT IN (
                       SELECT entity_id FROM entity_aliases
                        WHERE graph_generation_id=?
                       UNION
                       SELECT subject_entity_id FROM claims
                        WHERE graph_generation_id=?
                       UNION
                       SELECT object_entity_id FROM claims
                        WHERE graph_generation_id=? AND object_entity_id IS NOT NULL
                     )
                     AND id NOT IN (
                       SELECT target_key FROM graph_overrides
                        WHERE target_type='entity'
                     )""",
                (iso(utc_now()), run_id, run_id, run_id),
            )
            self.conn.execute(
                """UPDATE graph_runs SET state='superseded'
                   WHERE state='active' AND id<>?""",
                (run_id,),
            )
            now = iso(utc_now())
            self.conn.execute(
                """UPDATE graph_runs SET state='active', completed_at=?, activated_at=?
                   WHERE id=?""",
                (now, now, run_id),
            )
            self.conn.execute("UPDATE summaries SET dirty=1")
            emit_event(
                self.conn,
                "graph_generation_activated",
                "graph_run",
                run_id,
                {
                    "generation_key": run["generation_key"],
                    "extractor_version": run["extractor_version"],
                },
            )

    def _coverage_counts(self, run_id: str) -> tuple[int, int]:
        row = self.conn.execute(
            """SELECT COUNT(DISTINCT c.id) AS claims, COUNT(e.id) AS evidence
                 FROM claims c
                 LEFT JOIN claim_evidence e ON e.claim_id=c.id
                WHERE c.graph_generation_id=?""",
            (run_id,),
        ).fetchone()
        return int(row["claims"]), int(row["evidence"])

    def rollback(self, run_id: str) -> None:
        """Reactivate one previously approved generation without rebuilding it."""
        with self.conn:
            target = self.conn.execute(
                "SELECT * FROM graph_runs WHERE id=?", (run_id,)
            ).fetchone()
            if target is None or target["state"] != "superseded":
                raise RuntimeError("rollback target is not a superseded graph run")
            active = self.conn.execute(
                "SELECT id FROM graph_runs WHERE state='active'"
            ).fetchone()
            if active is None:
                raise RuntimeError("there is no active graph to roll back")
            now = iso(utc_now())
            self.conn.execute(
                "UPDATE graph_runs SET state='superseded' WHERE id=?", (active["id"],)
            )
            self.conn.execute(
                "UPDATE graph_runs SET state='active', activated_at=? WHERE id=?",
                (now, run_id),
            )
            self.conn.execute(
                """UPDATE entities SET status='active', updated_at=?
                    WHERE id IN (
                      SELECT entity_id FROM entity_aliases WHERE graph_generation_id=?
                      UNION
                      SELECT subject_entity_id FROM claims WHERE graph_generation_id=?
                      UNION
                      SELECT object_entity_id FROM claims
                       WHERE graph_generation_id=? AND object_entity_id IS NOT NULL
                    )""",
                (now, run_id, run_id, run_id),
            )
            self.conn.execute("UPDATE summaries SET dirty=1")
            emit_event(
                self.conn,
                "graph_generation_rolled_back",
                "graph_run",
                run_id,
                {"superseded_generation_id": active["id"]},
            )

    def fail(self, run_id: str, error: str) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE graph_runs SET state='failed', error=?, completed_at=?
                   WHERE id=? AND state='staging'""",
                (error[:2000], iso(utc_now()), run_id),
            )

    def _detect_relations(self, run_id: str) -> None:
        rows = self.conn.execute(
            """SELECT * FROM claims WHERE graph_generation_id=?
               AND lifecycle_state IN ('candidate','active')
               ORDER BY subject_entity_id, predicate, id""",
            (run_id,),
        ).fetchall()
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(
                (row["subject_entity_id"], row["predicate"]), []
            ).append(row)
        for group in grouped.values():
            for left, right in combinations(group, 2):
                relation = claim_relation(dict(left), dict(right))
                if relation is None:
                    continue
                from_claim, to_claim = left, right
                if relation == "supersedes":
                    if right["modality"] == "fact":
                        from_claim, to_claim = right, left
                    self.conn.execute(
                        """UPDATE claims SET plan_state='completed',
                           lifecycle_state='superseded', updated_at=?
                           WHERE id=? AND modality='plan'""",
                        (iso(utc_now()), to_claim["id"]),
                    )
                fingerprint = hashlib.sha256(
                    f"{from_claim['fingerprint']}\0{to_claim['fingerprint']}\0"
                    f"{relation}".encode()
                ).hexdigest()
                self.conn.execute(
                    """INSERT OR IGNORE INTO claim_relations
                       (id, from_claim_id, to_claim_id, relation_type, confidence,
                        explanation, graph_generation_id, fingerprint, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id(),
                        from_claim["id"],
                        to_claim["id"],
                        relation,
                        min(left["confidence"], right["confidence"]),
                        "Same subject, predicate, and overlapping validity window.",
                        run_id,
                        fingerprint,
                        iso(utc_now()),
                    ),
                )

    def run_result(self, run_id: str, *, no_op: bool = False) -> RebuildResult:
        row = self.conn.execute(
            "SELECT * FROM graph_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        counts = self.conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM claims WHERE graph_generation_id=?) AS claims,
                 (SELECT COUNT(*) FROM entity_aliases WHERE graph_generation_id=?) AS aliases,
                 (SELECT COUNT(*) FROM predicate_quarantine
                    WHERE graph_generation_id=?) AS quarantined""",
            (run_id, run_id, run_id),
        ).fetchone()
        return RebuildResult(
            run_id=run_id,
            generation_key=row["generation_key"],
            entries_total=row["entries_total"],
            entries_processed=row["entries_processed"],
            claims=counts["claims"],
            aliases=counts["aliases"],
            quarantined=counts["quarantined"],
            no_op=no_op,
        )

    def claims(
        self,
        *,
        entity_id: str | None = None,
        predicate: str | None = None,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[Claim]:
        run = self.active_run()
        if run is None:
            return []
        where = ["graph_generation_id=?"]
        params: list = [run.id]
        if not include_inactive:
            where.append("lifecycle_state IN ('active','superseded')")
        if entity_id:
            where.append("(subject_entity_id=? OR object_entity_id=?)")
            params.extend((entity_id, entity_id))
        if predicate:
            where.append("predicate=?")
            params.append(predicate)
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT * FROM claims WHERE {' AND '.join(where)}
                ORDER BY COALESCE(valid_from, asserted_at, observed_at) DESC,
                         confidence DESC LIMIT ?""",
            params,
        ).fetchall()
        return [_row_to_claim(row) for row in rows]

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self.conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        return _row_to_claim(row) if row else None

    def evidence_for_claim(self, claim_id: str) -> list[EvidenceLink]:
        rows = self.conn.execute(
            "SELECT * FROM claim_evidence WHERE claim_id=? ORDER BY id", (claim_id,)
        ).fetchall()
        return [
            EvidenceLink(
                id=row["id"],
                claim_id=row["claim_id"],
                entry_id=row["entry_id"],
                chunk_id=row["chunk_id"],
                quote=row["quote"],
                speaker_entity_id=row["speaker_entity_id"],
                evidence_role=row["evidence_role"],
                confidence=row["confidence"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                sensitivity=row["sensitivity"],
                graph_generation_id=row["graph_generation_id"],
                fingerprint=row["fingerprint"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def entities(
        self, *, status: str = "active", entity_type: str | None = None
    ) -> list[Entity]:
        where = ["status=?"]
        params: list = [status]
        if entity_type:
            where.append("entity_type=?")
            params.append(entity_type)
        rows = self.conn.execute(
            f"""SELECT * FROM entities WHERE {' AND '.join(where)}
                ORDER BY canonical_label, id"""
            ,
            params,
        ).fetchall()
        return [
            Entity(
                id=row["id"],
                entity_type=row["entity_type"],
                canonical_label=row["canonical_label"],
                status=row["status"],
                merged_into_id=row["merged_into_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                graph_generation_id=row["graph_generation_id"],
                generation_version=row["generation_version"],
            )
            for row in rows
        ]

    def add_relation(
        self,
        from_claim_id: str,
        to_claim_id: str,
        relation_type: ClaimRelationType,
        confidence: float,
        explanation: str = "",
    ) -> str:
        if relation_type in {
            ClaimRelationType.CAUSED_BY,
            ClaimRelationType.CORRELATES_WITH,
        }:
            modality = self.conn.execute(
                "SELECT modality FROM claims WHERE id=?", (from_claim_id,)
            ).fetchone()
            if modality is None or modality["modality"] != "inference":
                raise ValueError("causal and correlation relations require inference claims")
        run = self.active_run()
        if run is None:
            raise RuntimeError("no active graph")
        fingerprint = hashlib.sha256(
            f"{from_claim_id}\0{to_claim_id}\0{relation_type}".encode()
        ).hexdigest()
        relation_id = new_id()
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO claim_relations
                   (id, from_claim_id, to_claim_id, relation_type, confidence,
                    explanation, graph_generation_id, fingerprint, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    relation_id,
                    from_claim_id,
                    to_claim_id,
                    relation_type,
                    confidence,
                    explanation,
                    run.id,
                    fingerprint,
                    iso(utc_now()),
                ),
            )
        row = self.conn.execute(
            """SELECT id FROM claim_relations
               WHERE graph_generation_id=? AND fingerprint=?""",
            (run.id, fingerprint),
        ).fetchone()
        return row["id"]
