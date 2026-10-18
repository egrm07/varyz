"""Shipping gate: hostile archive text stays source-addressed, inert evidence.

The checked-in corpus is synthetic and public.  The gate drives the same
document connector, ingest transaction, chunk/embed code, hybrid search,
``recall`` function, and MCP JSON-RPC adapter used by the product.  Only the
embedder is fake: it is deterministic, keyless, and exercises the real vector
index without downloading a model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from varyz.config import Config, StorageConfig
from varyz.connectors.documents import MAX_DOCUMENT_CHARS, DocumentsConnector
from varyz.db import dao
from varyz.db.connection import open_db
from varyz.domain import Kind, Sensitivity
from varyz.mcp_server import VaryzServer, handle
from varyz.pipeline.embed import embed_pending
from varyz.pipeline.ingest import sync_connector
from varyz.retrieval.answer import recall
from varyz.retrieval.search import hybrid_search
from varyz.vault import Vault

CORPUS = Path(__file__).parent / "fixtures" / "hostile_documents.json"
EXPECTED_THREATS = {
    "instruction_injection",
    "fake_system_prompt",
    "invisible_unicode",
    "enormous_single_line",
    "repetition_bomb_shape",
}
_WORD = re.compile(r"\w+", re.UNICODE)


class _DeterministicLexicalEmbedder:
    """Small keyless vectorizer for the gate's real sqlite-vec path.

    The suite-wide fake intentionally only recognizes identical strings. This
    one hashes token presence into a normalized fixed-width vector, so a
    question is close to the chunk containing its unique answer terms without
    pretending a downloaded model or network service took part in the test.
    """

    model_name = "hostile-gate-lexical-384"

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * 384
        for token in set(_WORD.findall(text.casefold())):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:2], "big") % len(vector)] = 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _load_corpus() -> list[dict[str, Any]]:
    manifest = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    cases = manifest["cases"]
    assert {case["threat"] for case in cases} == EXPECTED_THREATS
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["filename"] for case in cases}) == len(cases)
    return cases


def _body(case: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in case["parts"]:
        if "text" in part:
            chunks.append(part["text"])
        else:
            chunks.append(part["repeat"] * int(part["count"]))
    return "".join(chunks)


def _mcp_call(server: VaryzServer, question: str) -> dict[str, Any]:
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "recall", "arguments": {"question": question}},
        },
    )
    assert response is not None
    assert "error" not in response
    return response["result"]


def test_hostile_document_corpus_is_inert_through_the_real_recall_path(
    tmp_path: Path,
) -> None:
    cases = _load_corpus()
    embedder = _DeterministicLexicalEmbedder()
    source_dir = tmp_path / "hostile-public-corpus"
    source_dir.mkdir()
    original_bodies: dict[str, str] = {}
    for case in cases:
        body = _body(case)
        original_bodies[case["id"]] = body
        (source_dir / case["filename"]).write_text(body, encoding="utf-8")

    cfg = Config(
        storage=StorageConfig(data_dir=tmp_path / "data"),
        connectors={
            "documents": {"approved": True, "paths": [str(source_dir)]},
        },
        base_dir=tmp_path,
    )
    cfg.retrieval.distill = "off"
    conn = open_db(cfg.db_path)
    server: VaryzServer | None = None
    try:
        stats = sync_connector(
            conn,
            Vault(cfg.vault_dir),
            DocumentsConnector(),
            cfg,
        )
        assert (stats.new, stats.errors) == (len(cases), 0)

        embedded = embed_pending(conn, cfg, embedder)
        assert embedded > len(cases), "stress documents must exercise real chunking"

        stored = {
            Path(row["external_id"]).name: row
            for row in conn.execute(
                "SELECT id, source, external_id, kind, sensitivity, body FROM entries"
            )
        }
        assert set(stored) == {case["filename"] for case in cases}
        for case in cases:
            row = stored[case["filename"]]
            # Hostile wording cannot promote itself into an instruction or a
            # new resource type.  It remains an ordinary external document.
            assert row["source"] == "documents"
            assert row["kind"] == str(Kind.DOCUMENT)
            assert row["sensitivity"] == str(Sensitivity.PERSONAL)
            assert case["expected"] in row["body"]

        huge = stored["04-enormous-single-line.txt"]
        repeated = stored["05-repetition-bomb-shape.md"]
        assert "\n" not in original_bodies["enormous-single-line"]
        assert len(original_bodies["enormous-single-line"]) > MAX_DOCUMENT_CHARS
        assert len(original_bodies["repetition-bomb-shape"]) > MAX_DOCUMENT_CHARS
        assert len(huge["body"]) < len(original_bodies["enormous-single-line"])
        assert len(repeated["body"]) < len(original_bodies["repetition-bomb-shape"])

        server = VaryzServer()
        server._cfg = cfg
        server._conn = conn
        server._embedder = embedder
        server._embedder_loaded = True

        handshake = handle(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
        )
        assert handshake is not None
        assert "data, never instruction" in handshake["result"]["instructions"]
        listing = handle(
            server,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )
        assert listing is not None
        recall_tool = next(
            tool for tool in listing["result"]["tools"] if tool["name"] == "recall"
        )
        assert "quoted data, not instruction" in recall_tool["description"]

        writes_before_retrieval = conn.total_changes
        for case in cases:
            expected_path = str((source_dir / case["filename"]).resolve())

            hits = hybrid_search(conn, case["query"], embedder, limit=8)
            assert any(hit.entry.external_id == expected_path for hit in hits), case["id"]

            direct = recall(conn, cfg, case["query"], embedder=embedder)
            direct_capsule = next(
                capsule
                for capsule in direct.capsules
                if capsule.external_id == expected_path
            )
            assert direct_capsule.provenance == "external"
            assert direct_capsule.source == "documents"
            assert direct_capsule.kind == str(Kind.DOCUMENT)
            assert direct_capsule.sensitivity == str(Sensitivity.PERSONAL)
            assert case["expected"] in direct_capsule.text

            mcp = _mcp_call(server, case["query"])
            payload = mcp["structuredContent"]
            capsule = next(
                item for item in payload["evidence"] if item["reference"] == expected_path
            )
            assert capsule["provenance"] == "external"
            assert capsule["source"] == "documents"
            assert capsule["kind"] == str(Kind.DOCUMENT)
            assert capsule["sensitivity"] == str(Sensitivity.PERSONAL)
            assert capsule["entry_id"] == stored[case["filename"]]["id"]
            assert case["expected"] in capsule["text"]
            # The legacy text rendering must carry the exact same labels and
            # address; otherwise an older MCP client would lose the boundary.
            assert json.loads(mcp["content"][0]["text"]) == payload

        # Retrieval has no write or action surface: directives merely travel
        # inside an externally-provenanced quotation.
        assert conn.total_changes == writes_before_retrieval
        assert dao.list_todos(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == len(cases)
    finally:
        if server is not None:
            # The server owns the injected connection once installed above.
            server.close()
        else:
            conn.close()
