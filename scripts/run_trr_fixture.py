"""Run the resolver offline against the frozen synthetic Varyz use-case corpus."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path

from varyz.config import Config, StorageConfig
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, NormalizedEntry, Sensitivity
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import ResolutionRequest
from varyz.resolution.resolver import EvidenceTier, ResolutionResolver
from varyz.trust import register_agent

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "varys_use_cases_v1.jsonl"
DEFAULT_CORPUS = ROOT / "evals" / "varys_use_cases_corpus_v1.jsonl"


def _rows(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def run(cases_path: Path, corpus_path: Path, *, split: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="varyz-trr-") as directory:
        root = Path(directory)
        cfg = Config(storage=StorageConfig(data_dir=root / "data"), base_dir=root)
        conn = open_db(cfg.db_path)
        try:
            with conn:
                for row in _rows(corpus_path):
                    occurred = datetime.fromisoformat(str(row["occurred"]).replace("Z", "+00:00"))
                    normalized = NormalizedEntry(
                        kind=Kind(str(row["kind"])),
                        external_id=str(row["external_id"]),
                        title=str(row.get("title") or ""),
                        body=str(row["body"]),
                        occurred_start=occurred,
                        captured_at=occurred,
                        authored_by_user=bool(row.get("authored_by_user", True)),
                    )
                    entry_id, _ = dao.upsert_entry(
                        conn,
                        normalized,
                        source=str(row["source"]),
                        sensitivity=Sensitivity(str(row.get("sensitivity", "personal"))),
                    )
                    dao.replace_chunks(
                        conn,
                        entry_id,
                        [f"{normalized.title}\n\n{normalized.body}"],
                    )

            caller = register_agent(
                conn,
                agent_id="agent:trr",
                display_name="Offline TRR harness",
                purpose="owner_context",
                device_id="fixture",
            ).caller
            resolver = ResolutionResolver(
                (SQLiteKnowledgeStore(conn), EvidenceTier(conn, cfg))
            )
            resolutions = []
            for case in _rows(cases_path):
                if str(case.get("split", "dev")) != split:
                    continue
                result = resolver.resolve(
                    ResolutionRequest(
                        question=str(case["question"]),
                        caller=caller,
                        purpose=caller.purpose,
                        allow_escalation=False,
                    )
                )
                payload = result.model_dump(mode="json")
                payload["case_id"] = str(case["id"])
                resolutions.append(payload)
            return {"suite": cases_path.stem, "split": split, "resolutions": resolutions}
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--split", choices=("dev", "held_out"), default="dev")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    payload = run(args.cases, args.corpus, split=args.split)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
