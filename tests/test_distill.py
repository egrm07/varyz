"""Cloud context is minimal: only matching chunks by default, and a local LLM
distills to just the needed excerpts when available."""

from __future__ import annotations

import json

import pytest
from varyz.retrieval.rag import ask

from varyz.config import Config
from varyz.connectors.manual import ManualConnector
from varyz.domain import Kind, SearchHit
from varyz.pipeline.embed import embed_pending
from varyz.pipeline.ingest import ingest_item
from varyz.pipeline.sensitivity import SensitivityGate
from varyz.retrieval.distill import distill


class FakeLocal:
    """Stands in for local inference: returns a scripted JSON selection."""

    def __init__(self, keep):
        self.keep = keep
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return json.dumps({"keep": self.keep})


def _hit(body: str, chunk: str | None = None) -> SearchHit:
    from varyz.domain import Entry, iso, utc_now

    e = Entry(id="e1", kind=Kind.NOTE, source="manual", external_id="x",
              content_hash="h", body=body, ingested_at=iso(utc_now()))
    return SearchHit(entry=e, score=1.0, chunk=chunk or body)


# ---- distill unit ------------------------------------------------------------


def test_distill_keeps_only_selected_and_trims():
    hits = [_hit("A: got the job offer today"), _hit("B: weather was nice"),
            _hit("C: bought milk")]
    local = FakeLocal(keep=[{"i": 1, "excerpt": "got the job offer"}])
    out = distill("did I get a job?", hits, local, max_keep=8)
    assert len(out) == 1
    assert out[0].hit is hits[0]
    assert out[0].text == "got the job offer"


def test_distill_ignores_out_of_range_and_dupes():
    hits = [_hit("only one")]
    local = FakeLocal(keep=[{"i": 5, "excerpt": "x"}, {"i": 1, "excerpt": "a"},
                            {"i": 1, "excerpt": "dupe"}])
    out = distill("q", hits, local, max_keep=8)
    assert [e.text for e in out] == ["a"]


def test_distill_bad_json_returns_empty():
    class Bad:
        def complete(self, s, u):
            return "sorry, I cannot help with that"
    assert distill("q", [_hit("x")], Bad(), max_keep=8) == []


# ---- ask pipeline: minimal context ------------------------------------------


def _seed(conn, vault, cfg, fake_embedder, texts):
    gate = SensitivityGate(cfg.sensitivity)
    for t in texts:
        ingest_item(conn, vault, ManualConnector(), gate, ManualConnector.make_item(t))
    embed_pending(conn, cfg, fake_embedder)


def test_ask_sends_only_chunks_not_full_history(conn, vault, cfg, fake_embedder, fake_llm):
    from varyz.llm.router import LLMRouter

    texts = [f"diary entry number {i} about ordinary day {i}" for i in range(40)]
    texts.append("On 2025-03-02 I signed the lease for the Berlin apartment.")
    _seed(conn, vault, cfg, fake_embedder, texts)
    cfg.retrieval.distill = "off"  # exercise the always-on chunk path

    router = LLMRouter(cfg, provider=fake_llm)
    result = ask(conn, cfg, "when did I sign the Berlin lease?", fake_embedder, router=router)
    list(result.stream)

    # Only a handful of excerpts reached the model, never all 41 entries.
    assert result.sent <= cfg.retrieval.send_k
    assert result.considered <= cfg.retrieval.send_k  # distill off → candidate_k == send_k
    _, user_prompt = fake_llm.calls[0]
    assert user_prompt.count("diary entry number") < 41
    assert result.chars_sent < 4000


def test_ask_uses_local_distillation_when_provided(conn, vault, cfg, fake_embedder, fake_llm):
    from varyz.llm.router import LLMRouter

    _seed(conn, vault, cfg, fake_embedder,
          ["signed the Berlin lease on 2025-03-02", "had pasta for lunch", "it rained"])
    local = FakeLocal(keep=[{"i": 1, "excerpt": "signed the Berlin lease on 2025-03-02"}])
    router = LLMRouter(cfg, provider=fake_llm)

    result = ask(conn, cfg, "berlin lease?", fake_embedder, router=router, local_provider=local)
    list(result.stream)

    assert result.distilled is True
    assert result.sent == 1
    assert local.calls  # the local model actually ran
    _, user_prompt = fake_llm.calls[0]
    assert "Berlin lease" in user_prompt
    assert "pasta" not in user_prompt  # irrelevant candidate never sent to cloud


def test_ask_never_starts_an_installed_local_model_implicitly(
    conn, vault, cfg, fake_embedder, fake_llm, monkeypatch
):
    """An ordinary read must not reserve the GPU as a hidden side effect.

    This is deliberately tested with the legacy ``auto`` value: existing
    archives may still carry it after upgrading, and those are the archives
    most likely to have a managed model installed already.
    """
    from varyz.llm.router import LLMRouter

    _seed(conn, vault, cfg, fake_embedder, ["signed the Berlin lease on 2025-03-02"])
    cfg.retrieval.distill = "auto"

    def refuse(*_args, **_kwargs):
        pytest.fail("ordinary ask constructed the managed local provider")

    monkeypatch.setattr("varyz.llm.local.build_local_provider", refuse)
    router = LLMRouter(cfg, provider=fake_llm)

    result = ask(conn, cfg, "berlin lease?", fake_embedder, router=router)
    list(result.stream)

    assert result.distilled is False
    assert result.sent >= 1


def test_gpu_heavy_background_features_are_safe_by_default():
    cfg = Config()

    assert cfg.retrieval.distill == "off"
    assert cfg.autosync.semantic_graph is False


def test_ask_distill_failure_falls_back_to_chunks(conn, vault, cfg, fake_embedder, fake_llm):
    from varyz.llm.router import LLMRouter

    _seed(conn, vault, cfg, fake_embedder, ["signed the Berlin lease on 2025-03-02"])

    class Broken:
        def complete(self, s, u):
            raise RuntimeError("ollama down")

    router = LLMRouter(cfg, provider=fake_llm)
    result = ask(conn, cfg, "berlin?", fake_embedder, router=router, local_provider=Broken())
    list(result.stream)
    assert result.distilled is False
    assert result.sent >= 1  # fell back, still answered


def test_ask_still_excludes_sensitive_after_distill(conn, vault, cfg, fake_embedder, fake_llm):
    from varyz.llm.router import LLMRouter

    cfg.sensitivity.rules.patterns = [r"\bdiagnosis\b"]
    gate = SensitivityGate(cfg.sensitivity)
    ingest_item(conn, vault, ManualConnector(), gate,
                ManualConnector.make_item("the diagnosis about my knee"))
    ingest_item(conn, vault, ManualConnector(), gate,
                ManualConnector.make_item("went jogging, knee is fine"))
    embed_pending(conn, cfg, fake_embedder)

    # Local model would keep everything it sees — but sensitive is filtered
    # out BEFORE distillation, so it never sees the diagnosis.
    local = FakeLocal(keep=[{"i": 1, "excerpt": "went jogging"}])
    router = LLMRouter(cfg, provider=fake_llm)
    result = ask(conn, cfg, "knee?", fake_embedder, router=router, local_provider=local)
    list(result.stream)

    assert result.excluded_sensitive == 1
    _, local_prompt = local.calls[0]
    assert "diagnosis" not in local_prompt  # sensitive never reaches even the local step
