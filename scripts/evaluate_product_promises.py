"""Baseline behavioral probes; synthetic state only, no production writes or model calls."""
# ruff: noqa: E402 -- standalone source-tree runner sets sys.path before package imports.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.mcp_server import VaryzServer
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.briefing import finish_check, start_task
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.session_progress import Checkpoint, HistoryQuery, history, record
from varyz.trust import register_agent

RUN_DATE = datetime.now(UTC).date().isoformat()
OUT = ROOT / "reports" / f"promises_baseline_{RUN_DATE}.json"
OWNER = Caller(
    agent_id="owner:synthetic",
    display_name="Synthetic owner",
    purpose="owner_knowledge",
    device_id="device:eval",
    transport="stdio",
)
rows = []


def check(promise, name, passed, observed, expected):
    rows.append(
        dict(
            promise=promise,
            scenario=name,
            passed=bool(passed),
            observed=observed,
            expected=expected,
        )
    )


def compact(result):
    value = {
        k: result[k]
        for k in ("disposition", "answer", "basis", "error", "agent_decides")
        if k in result
    }
    for key in ("ask_owner", "deferred_owner"):
        if key in result:
            value[key] = {
                k: result[key][k]
                for k in ("question", "kind", "proposed_choice")
                if k in result[key]
            }
    return value


def run(window):
    import tempfile

    with tempfile.TemporaryDirectory(
        prefix="promises-", dir=ROOT / ".pytest-tmp", ignore_cleanup_errors=True
    ) as folder:
        root = Path(folder)
        cfgpath = root / "config.toml"
        cfgpath.write_text(
            f'[storage]\ndata_dir="{(root / "data").as_posix()}"\n'
            '[workflows]\nparaphrase_verifier="off"\n',
            encoding="utf-8",
        )
        cfg = load_config(cfgpath)
        project = "cedar" if window == "primary" else "juniper"
        with closing(open_db(cfg.db_path)) as conn:
            rules = SQLitePrincipleStore(conn)
            old = rules.add(
                statement="Every website must include a favicon.",
                domain="website",
                caller=OWNER,
                device_id="device:eval",
                source="synthetic-eval",
            )
            local = rules.add(
                statement="Use the amber wordmark for this product.",
                scope=project,
                caller=OWNER,
                device_id="device:eval",
                source="synthetic-eval",
            )
            SQLiteKnowledgeStore(conn).remember(
                question=f"Which accent colour should {project} use?",
                answer="Amber",
                scope=project,
                caller=OWNER,
                basis=AnswerBasis.EXPLICIT,
                source="synthetic-eval",
                device_id="device:eval",
            )
            conn.commit()
        # Closing and reopening storage tests a fresh task rather than a same-object cache.
        with closing(open_db(cfg.db_path)) as conn:
            b = start_task(conn, "Build a website", project=project, cwd=root)
            ids = {x["id"] for x in b["definition_of_done"].get("website", [])}
            check(
                "habits",
                window + ": persisted standard",
                old in ids,
                sorted(ids),
                "Saved favicon rule reaches a fresh task",
            )
            other = start_task(conn, "Build a website", project="other", cwd=root)
            ids2 = {x["id"] for x in other["project_rules"]}
            check(
                "habits",
                window + ": scope isolation",
                local not in ids2,
                sorted(ids2),
                "Product wordmark stays in its project",
            )
            new = SQLitePrincipleStore(conn).add(
                statement="Every website must include an SVG favicon.",
                domain="website",
                supersedes_id=old,
                caller=OWNER,
                device_id="device:eval",
                source="synthetic-eval",
            )
            b = start_task(conn, "Build a website", project=project, cwd=root)
            ids = {x["id"] for x in b["definition_of_done"].get("website", [])}
            check(
                "habits",
                window + ": correction replaces old",
                new in ids and old not in ids,
                sorted(ids),
                "Only corrected standard remains active",
            )
            record(
                conn,
                Checkpoint(
                    project=project,
                    conversation_id="previous-task",
                    checkpoint_id="last",
                    task="Build website",
                    status="blocked",
                    done=["Implemented pricing page"],
                    pending=["Domain verification required"],
                ),
                agent_id="agent:codex:eval",
            )
            resumed = start_task(
                conn, "Continue the website from last session", project=project, cwd=root
            )
            serialized = json.dumps(resumed)
            check(
                "continuity",
                window + ": automatic resume",
                "Domain verification required" in serialized,
                {
                    "contains_last_blocker": "Domain verification required" in serialized,
                    "history_status": resumed["history_check"]["status"],
                },
                "Fresh-task briefing carries last recorded blocker",
            )
            h = history(conn, HistoryQuery(project=project, agent="codex", latest=True))
            check(
                "continuity",
                window + ": explicit resume",
                h["found"] == 1 and h["reports"][0]["pending"] == ["Domain verification required"],
                {"found": h["found"], "pending": h["reports"][0]["pending"] if h["found"] else []},
                "Explicit session retrieval returns current blocker",
            )
            # The same standards call sees neither the compliant nor incomplete artifact.
            (root / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            (root / "index.html").write_text('<html><link rel="icon" href="icon.svg"></html>')
            good = finish_check(conn, "Build a website", project=project, cwd=root)
            (root / "index.html").write_text("<html>No favicon</html>")
            bad = finish_check(conn, "Build a website", project=project, cwd=root)
            check(
                "standards",
                window + ": supplies standard",
                new in {x["id"] for x in bad["definition_of_done"]},
                [x["item"] for x in bad["definition_of_done"]],
                "Corrected standard appears before completion",
            )
            check(
                "standards",
                window + ": detects artifact violation",
                good != bad,
                {"identical_for_compliant_and_incomplete_artifacts": good == bad},
                "A quality verifier distinguishes a missing favicon from a present one",
            )
            conn.commit()
        daemon = build_resolution_daemon(
            cfg.db_path, host="127.0.0.1", port=0, device_id="device:eval"
        )
        with daemon:
            host, port = daemon.address
            server = VaryzServer(
                cfgpath, resolution_client=ResolutionClient(host, port, archive=cfg.db_path)
            )
            credential = register_agent(
                server.conn, agent_id="agent:eval", display_name="Synthetic evaluation"
            )
            server._agent_token = credential.token
            server._task_sizer = lambda *args, **kwargs: None

            def ask(q, **extra):
                return server.call(
                    "ask", dict(question=q, project=project, conversation_id="fresh-task", **extra)
                )

            try:
                queries = [
                    f"Which accent colour should {project} use?",
                    f"What accent color should {project} use?",
                    "Which highlight hue belongs on the product?",
                ]
                for i, q in enumerate(queries):
                    r = ask(q)
                    check(
                        "fewer_prompts",
                        f"{window}: known decision wording {i + 1}",
                        r.get("answer") == "Amber",
                        {"question": q, "result": compact(r)},
                        "Reuse known colour without asking owner again",
                    )
                r = ask("Which test runner should we use?", decision_kind="delegated")
                check(
                    "fewer_prompts",
                    window + ": delegated unknown",
                    bool(r.get("agent_decides")) and not r.get("ask_owner"),
                    compact(r),
                    "Routine unknown delegated choice requires no owner prompt",
                )
                existing_pending = server.conn.execute(
                    "SELECT COUNT(*) FROM resolution_escalations WHERE status='pending'"
                ).fetchone()[0]
                server.call(
                    "away",
                    dict(
                        action="on",
                        instruction="Keep working while I am away",
                        project=project,
                        conversation_id="fresh-task",
                        scope="task",
                    ),
                )
                r = ask(queries[0])
                check(
                    "away",
                    window + ": known answer while away",
                    r.get("answer") == "Amber",
                    compact(r),
                    "Recorded choice reused",
                )
                r = ask(
                    "Which export layout should this product use?", proposed_choice="Single column"
                )
                check(
                    "away",
                    window + ": unknown deferred",
                    bool(r.get("deferred_owner")) and not r.get("ask_owner"),
                    compact(r),
                    "Unknown preference queued with provisional choice",
                )
                r = ask("May I publish this product?", decision_kind="permission")
                check(
                    "away",
                    window + ": permission never invented",
                    r.get("answer") is None
                    and r.get("deferred_owner", {}).get("kind") == "permission",
                    compact(r),
                    "Publishing waits for owner permission",
                )
                r = ask("Which test runner should we use?", decision_kind="delegated")
                check(
                    "away",
                    window + ": delegated work continues",
                    bool(r.get("agent_decides")),
                    compact(r),
                    "Routine engineering continues without a prompt",
                )
                returned = server.call(
                    "away",
                    dict(
                        action="off",
                        instruction="I'm back",
                        project=project,
                        conversation_id="fresh-task",
                    ),
                )
                check(
                    "away",
                    window + ": return retains queue",
                    returned.get("review", {}).get("pending") == existing_pending + 2,
                    {
                        "pending": returned.get("review", {}).get("pending"),
                        "before_away": existing_pending,
                    },
                    "Existing unresolved questions plus two new owner decisions survive return",
                )
            finally:
                server.close()
                os.chdir(ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUT)
    output = parser.parse_args().output
    os.environ.pop("CODEX_THREAD_ID", None)
    (ROOT / ".pytest-tmp").mkdir(exist_ok=True)
    for window in ("primary", "fresh_storage_repeat"):
        run(window)
    result = dict(
        date=RUN_DATE,
        kind="synthetic behavioral baseline",
        rows=rows,
        passed=sum(r["passed"] for r in rows),
        failed=sum(not r["passed"] for r in rows),
        caveats=[
            "Fresh-storage repeat tests reproducibility, not independent semantic generalization.",
            "No agent-built deliverables or actual human prompt counts; "
            "no claimed productivity lift.",
            "Deterministic fallback, no local paraphrase verifier or paid model calls.",
            "Empty-memory prompt floor is one owner question per owner-specific unknown; "
            "fixed rule files are an unmeasured stronger baseline.",
        ],
    )
    result["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["source_sha256"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "src/varyz/mcp_server.py",
            "src/varyz/resolution/briefing.py",
            "src/varyz/resolution/knowledge.py",
            "src/varyz/resolution/away.py",
            "src/varyz/resolution/trials.py",
            "src/varyz/session_progress.py",
        )
    }
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "failed": result["failed"],
                "failures": [r["scenario"] for r in rows if not r["passed"]],
                "artifact": str(output),
            }
        )
    )
