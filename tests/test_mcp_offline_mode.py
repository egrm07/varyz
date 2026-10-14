"""Focused coverage for the clean-machine smoke's internal offline switch."""

from varyz.mcp_server import VaryzServer


def test_internal_ci_switch_keeps_mcp_retrieval_lexical(monkeypatch):
    monkeypatch.setenv("VARYZ_DISABLE_EMBEDDINGS", "1")
    server = VaryzServer()

    assert server.embedder is None


def test_internal_ci_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv("VARYZ_DISABLE_EMBEDDINGS", raising=False)
    server = VaryzServer()

    assert server.embedder is not None

