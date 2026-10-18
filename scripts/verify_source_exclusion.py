"""Fail if an active Varyz projection contains configured excluded sources."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.evidence.reader import SQLiteEvidenceReader


def _count(conn, sql: str, params: tuple[object, ...]) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def verify(config_path: Path, *, require_model: bool) -> dict[str, object]:
    cfg = load_config(config_path)
    excluded = tuple(sorted({item.strip().lower() for item in cfg.graph.excluded_sources}))
    if not excluded:
        raise RuntimeError("graph.excluded_sources is empty")
    placeholders = ",".join("?" for _ in excluded)
    conn = open_db(cfg.db_path)
    try:
        graph = conn.execute(
            """SELECT id,input_content_hash,entries_total,entries_processed
                 FROM graph_runs WHERE state='active'
                 ORDER BY activated_at DESC LIMIT 1"""
        ).fetchone()
        if graph is None:
            raise RuntimeError("no active graph generation")

        reader = SQLiteEvidenceReader(conn, excluded_sources=excluded)
        eligible_entries = _count(
            conn,
            f"SELECT COUNT(*) FROM entries WHERE lower(source) NOT IN ({placeholders})",  # noqa: S608
            excluded,
        )
        graph_evidence_violations = _count(
            conn,
            f"""SELECT COUNT(*) FROM claim_evidence ce
                 JOIN entries e ON e.id=ce.entry_id
                WHERE ce.graph_generation_id=?
                  AND lower(e.source) IN ({placeholders})""",  # noqa: S608
            (graph["id"], *excluded),
        )
        graph_alias_violations = _count(
            conn,
            f"""SELECT COUNT(*) FROM entity_aliases a
                 JOIN entries e ON e.id=a.source_entry_id
                WHERE a.graph_generation_id=?
                  AND lower(e.source) IN ({placeholders})""",  # noqa: S608
            (graph["id"], *excluded),
        )
        failures: list[str] = []
        if int(graph["entries_total"]) != eligible_entries:
            failures.append(
                f"graph entries_total={graph['entries_total']} but eligible "
                f"corpus={eligible_entries}"
            )
        if int(graph["entries_processed"]) != eligible_entries:
            failures.append(
                "active graph is incomplete: "
                f"processed={graph['entries_processed']} eligible={eligible_entries}"
            )
        fingerprint = reader.corpus_fingerprint()
        if graph["input_content_hash"] != fingerprint.content_hash:
            failures.append("active graph fingerprint does not match the eligible corpus")
        if graph_evidence_violations:
            failures.append(f"graph has {graph_evidence_violations} excluded evidence rows")
        if graph_alias_violations:
            failures.append(f"graph has {graph_alias_violations} excluded alias rows")

        result: dict[str, object] = {
            "excluded_sources": excluded,
            "eligible_entries": eligible_entries,
            "active_graph": graph["id"],
            "graph_excluded_evidence": graph_evidence_violations,
            "graph_excluded_aliases": graph_alias_violations,
        }
        conversation_violations = _count(
            conn,
            f"""SELECT COUNT(*) FROM conversation_windows
                WHERE lower(source) IN ({placeholders})""",  # noqa: S608
            excluded,
        )
        result["conversation_excluded_windows"] = conversation_violations
        if conversation_violations:
            failures.append(
                f"conversation partition has {conversation_violations} excluded-source windows"
            )

        active_model = conn.execute(
            """SELECT g.id,g.graph_generation_id,g.state,g.record_count
                 FROM personal_model_active_generation a
                 JOIN personal_model_generations g ON g.id=a.generation_id
                WHERE a.singleton=1"""
        ).fetchone()
        if active_model is None:
            result["active_model"] = None
            if require_model:
                failures.append("no active personal-model generation")
        else:
            generation_id = str(active_model["id"])
            if active_model["graph_generation_id"] != graph["id"]:
                failures.append("active personal model points at a non-active graph")
            model_evidence_violations = _count(
                conn,
                f"""SELECT COUNT(*) FROM personal_model_record_evidence r
                     JOIN entries e ON e.id=r.entry_id
                    WHERE r.generation_id=?
                      AND lower(e.source) IN ({placeholders})""",  # noqa: S608
                (generation_id, *excluded),
            )
            model_target_violations = _count(
                conn,
                f"""SELECT COUNT(*) FROM personal_model_target_episode_coverage c
                     JOIN entries e ON e.id=c.target_entry_id
                    WHERE c.generation_id=?
                      AND lower(e.source) IN ({placeholders})""",  # noqa: S608
                (generation_id, *excluded),
            )
            model_identity_violations = _count(
                conn,
                f"""SELECT COUNT(*) FROM personal_model_source_identity_memberships
                    WHERE generation_id=?
                      AND lower(source) IN ({placeholders})""",  # noqa: S608
                (generation_id, *excluded),
            )
            model_reached_violations = _count(
                conn,
                f"""SELECT COUNT(*)
                     FROM personal_model_observation_progress p,
                          json_each(p.reached_entry_ids_json) reached
                     JOIN entries e ON e.id=reached.value
                    WHERE p.generation_id=?
                      AND lower(e.source) IN ({placeholders})""",  # noqa: S608
                (generation_id, *excluded),
            )
            model_dependency_violations = _count(
                conn,
                f"""SELECT COUNT(*)
                     FROM personal_model_record_dependencies d
                     JOIN entries e ON e.id=d.dependency_id
                    WHERE d.generation_id=?
                      AND d.dependency_kind='entry'
                      AND lower(e.source) IN ({placeholders})""",  # noqa: S608
                (generation_id, *excluded),
            )
            model_handoff_violations = _count(
                conn,
                f"""SELECT COUNT(*)
                     FROM personal_model_action_handoffs h,
                          json_each(h.source_entry_ids_json) source_entry
                     JOIN entries e ON e.id=source_entry.value
                    WHERE h.generation_id=?
                      AND lower(e.source) IN ({placeholders})""",  # noqa: S608
                (generation_id, *excluded),
            )
            result.update(
                {
                    "active_model": generation_id,
                    "model_records": int(active_model["record_count"]),
                    "model_excluded_evidence": model_evidence_violations,
                    "model_excluded_targets": model_target_violations,
                    "model_excluded_identities": model_identity_violations,
                    "model_excluded_reached_entries": model_reached_violations,
                    "model_excluded_dependencies": model_dependency_violations,
                    "model_excluded_action_handoffs": model_handoff_violations,
                }
            )
            total_model_violations = sum(
                (
                    model_evidence_violations,
                    model_target_violations,
                    model_identity_violations,
                    model_reached_violations,
                    model_dependency_violations,
                    model_handoff_violations,
                )
            )
            if total_model_violations:
                failures.append(
                    f"active personal model has {total_model_violations} excluded-source links"
                )

        result["ok"] = not failures
        result["failures"] = failures
        if failures:
            raise RuntimeError(json.dumps(result, indent=2))
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--require-model", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.config, require_model=args.require_model), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
