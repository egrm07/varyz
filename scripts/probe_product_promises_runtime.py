"""Fresh running-engine probe. Imports the selected installed/staged package, not src/."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from contextlib import closing
from pathlib import Path

from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.mcp_server import VaryzServer
from varyz.personal_model.query import AnswerBasis
from varyz.resolution.client import ResolutionClient
from varyz.resolution.daemon import build_resolution_daemon
from varyz.resolution.knowledge import SQLiteKnowledgeStore
from varyz.resolution.models import Caller
from varyz.resolution.principles import SQLitePrincipleStore
from varyz.session_progress import Checkpoint, record


def run(output: Path) -> None:
    original_cwd = Path.cwd()
    os.environ.pop("CODEX_THREAD_ID", None)
    owner = Caller(
        agent_id="owner:synthetic",
        display_name="Synthetic owner",
        purpose="owner_knowledge",
        device_id="device:probe",
    )
    checks = {}
    with tempfile.TemporaryDirectory(
        dir=output.parent, prefix="runtime-", ignore_cleanup_errors=True
    ) as temporary:
        root = Path(temporary) / "acorn"
        root.mkdir()
        path = root / "config.toml"
        path.write_text(
            f'[storage]\ndata_dir="{(root / "data").as_posix()}"\n'
            '[workflows]\nparaphrase_verifier="off"\n',
            encoding="utf-8",
        )
        cfg = load_config(path)
        with closing(open_db(cfg.db_path)) as conn:
            rule = SQLitePrincipleStore(conn).add(
                statement="Include an SVG favicon.",
                domain="website",
                caller=owner,
                device_id="device:probe",
                source="synthetic-runtime-probe",
            )
            SQLiteKnowledgeStore(conn).remember(
                question="Which accent colour should Acorn use?",
                answer="Amber",
                scope="acorn",
                caller=owner,
                basis=AnswerBasis.EXPLICIT,
                device_id="device:probe",
                source="synthetic-runtime-probe",
            )
            record(
                conn,
                Checkpoint(
                    project="acorn",
                    conversation_id="old",
                    checkpoint_id="last",
                    task="Build website",
                    status="blocked",
                    done=["Pricing implemented"],
                    pending=["Domain verification"],
                ),
                agent_id="agent:codex:probe",
            )
            conn.commit()
        daemon = build_resolution_daemon(cfg.db_path, host="127.0.0.1", port=0)
        with daemon:
            host, port = daemon.address
            from varyz.trust import register_agent

            os.chdir(root)
            server = VaryzServer(
                path, resolution_client=ResolutionClient(host, port, archive=cfg.db_path)
            )
            server._agent_token = register_agent(
                server.conn, agent_id="agent:probe", display_name="Runtime probe"
            ).token
            server._task_sizer = lambda *args, **kwargs: None
            args = {"project": "acorn", "conversation_id": "new"}
            try:
                result = server.call(
                    "ask", {**args, "question": "Which highlight hue belongs on the product?"}
                )
                checks["decision_reused"] = result.get("answer") == "Amber"
                result = server.call("start_task", {**args, "task": "Resume last session"})
                checks["continuation_blocker"] = result["continuation"]["state"]["pending"] == [
                    "Domain verification"
                ]
                (root / "index.html").write_text('<link rel="icon" href="mark.svg">')
                (root / "mark.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
                result = server.call("finish_check", {**args, "task": "Build a website"})
                checks["artifact_verified"] = result["verification"]["independently_verified"]
                (root / "index.html").write_text("Missing icon")
                result = server.call(
                    "finish_check",
                    {
                        **args,
                        "task": "Build a website",
                        "evidence": [
                            {
                                "rule_id": rule,
                                "status": "passed",
                                "evidence": "Unsupported assertion",
                            }
                        ],
                    },
                )
                checks["missing_artifact_blocks"] = result["verification"]["failed"] == [rule]
                server.call(
                    "away", {**args, "action": "on", "instruction": "Keep working while I am away"}
                )
                result = server.call(
                    "ask", {**args, "question": "Which test runner?", "decision_kind": "delegated"}
                )
                checks["away_delegation"] = bool(result.get("agent_decides"))
                result = server.call(
                    "ask",
                    {**args, "question": "May I publish Acorn?", "decision_kind": "permission"},
                )
                checks["permission_deferred"] = result.get("answer") is None and bool(
                    result.get("deferred_owner")
                )
                checks["explicit_authority"] = (
                    SQLitePrincipleStore(server.conn).get(rule).advice()["authority"]
                    == "owner_confirmed"
                )
            finally:
                server.close()
                os.chdir(original_cwd)
    value = {
        "package": str(Path(__import__("varyz.mcp_server", fromlist=["x"]).__file__)),
        "checks": checks,
        "all_passed": all(checks.values()),
        "synthetic_only": True,
    }
    output.write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(json.dumps(value))
    if not value["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
