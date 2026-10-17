"""Mac setup offers operational cloud learning, with explicit source consent."""

from dataclasses import replace

import pytest

from tests.test_setup_web import _finished, _request, machine, server  # noqa: F401
from varyz import cli
from varyz.config import Config, load_config
from varyz.configfile import set_inference_mode
from varyz.resolution import rule_reader
from varyz.resolution.local_classifier import ClassifierUnavailable, local_model_from_config
from varyz.resolution.rule_reader import cloud_reader_from_config as real_cloud_reader
from varyz.setup_machine import GIB, Machine, memory, plan
from varyz.setup_web import SetupSession

MAC = Machine("Darwin", "arm64", 8, 16 * GIB, 8 * GIB, 40 * GIB)


@pytest.mark.parametrize("hardware", [MAC, replace(MAC, total_memory=8 * GIB),
                                      replace(MAC, architecture="x86_64")])
def test_cloud_choice_is_always_offered_on_mac(tmp_path, monkeypatch, hardware):
    monkeypatch.setattr("varyz.setup_machine.inspect_machine", lambda _: hardware)
    view = plan(tmp_path / "config.toml")
    assert "conversations" in view["choices"]
    assert view["cloud_conversations"] is False
    if hardware == MAC:
        assert view["choices"] == ["local", "conversations"]


def test_mac_memory_probe(monkeypatch):
    monkeypatch.setattr("varyz.setup_machine._output", lambda args:
                        str(16 * GIB) if args[0] == "sysctl" else
                        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                        "Pages free: 100.\nPages inactive: 200.\nPages speculative: 50.\n")
    assert memory("Darwin") == (16 * GIB, 350 * 16384)


def test_plan_api_has_both_choices(server):  # noqa: F811
    import json

    status, _, body = _request(server, "GET", "/api/plan", token=server.session.token)
    assert status == 200
    assert json.loads(body)["ai"]["choices"] == ["local", "conversations"]


@pytest.mark.parametrize("consent", [False, True])
def test_cloud_mode_is_saved_and_skips_ollama(server, machine, monkeypatch, consent):  # noqa: F811
    monkeypatch.setattr(cli, "_ready_classifier",
                        lambda *a, **kw: pytest.fail("cloud setup must not download Ollama"))
    status, _, body = _request(server, "POST", "/api/run", token=server.session.token,
                              body={"agents": ["claude-code"], "sources": ["ai_conversations"],
                                    "inference_mode": "conversations",
                                    "cloud_conversations": consent})
    assert status == 202, body
    result = _finished(server.session)["result"]
    assert result["verdict"] == "GO"
    cfg = load_config(machine.config)
    assert cfg.workflows.inference_mode == "conversations"
    assert cfg.workflows.rule_reader_cloud is consent
    assert cfg.workflows.rule_reader_agent is consent
    assert cfg.workflows.task_classifier == "embedding"
    assert "classifier" not in machine.finishing.calls
    assert rule_reader.reading_order(cfg, ("fits", "")) == (["cloud"] if consent else [])
    assert SetupSession(machine.config).plan()["ai"]["selected"] == "conversations"
    assert SetupSession(machine.config).plan()["ai"]["cloud_conversations"] is consent


@pytest.mark.parametrize("payload", [{"inference_mode": "invalid"},
                                     {"cloud_conversations": "yes"}])
def test_invalid_mode_does_not_start_setup(server, payload):  # noqa: F811
    status, _, _ = _request(server, "POST", "/api/run", token=server.session.token,
                            body={"agents": [], "sources": [], **payload})
    assert status == 400
    assert not server.session.started


def test_mode_switch_preserves_sources_and_revokes_cloud(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text('[connectors.documents]\napproved = true\n# keep me\n'
                      '[workflows]\nclassifier_model = "custom"\n', encoding="utf-8")
    set_inference_mode(target, "conversations", cloud_conversations=True)
    before = target.read_bytes()
    set_inference_mode(target, "conversations", cloud_conversations=True)
    assert target.read_bytes() == before
    set_inference_mode(target, "local", cloud_conversations=True)
    cfg = load_config(target)
    assert not cfg.workflows.rule_reader_cloud
    assert not cfg.workflows.rule_reader_agent
    assert cfg.workflows.classifier_model == "custom"
    assert cfg.connector_options("documents")["approved"]
    assert "# keep me" in target.read_text(encoding="utf-8")


def test_changing_existing_mode_restarts_daemon(machine):  # noqa: F811
    first = SetupSession(machine.config)
    assert first.start([], [], inference_mode="local") is None
    assert _finished(first)["result"]["verdict"] == "GO"
    second = SetupSession(machine.config)
    assert second.start([], [], inference_mode="conversations",
                        cloud_conversations=False) is None
    assert _finished(second)["result"]["verdict"] == "GO"
    assert machine.finishing.restarted == [machine.config]


def test_cloud_mode_never_probes_ollama(monkeypatch):
    cfg = Config()
    cfg.workflows.inference_mode = "conversations"
    monkeypatch.setattr("varyz.resolution.local_classifier.socket.create_connection",
                        lambda *a, **kw: pytest.fail("must not touch Ollama"))
    model = local_model_from_config(cfg)
    with pytest.raises(ClassifierUnavailable, match="disabled"):
        model.ready()
    with pytest.raises(ClassifierUnavailable, match="disabled"):
        model.size_bytes()
    assert rule_reader.local_fit(cfg)[0] == "no"


def test_cloud_reader_requires_mode_consent_and_approved_chats(monkeypatch):
    monkeypatch.setattr("varyz.llm.providers.build_provider",
                        lambda *a, **kw: pytest.fail("must not build cloud provider"))
    cfg = Config()
    cfg.workflows.inference_mode = "local"
    with pytest.raises(rule_reader.ReaderUnavailable, match="disabled"):
        real_cloud_reader(cfg, max_tokens=100)
    cfg.workflows.inference_mode = "conversations"
    cfg.workflows.rule_reader_cloud = False
    with pytest.raises(rule_reader.ReaderUnavailable, match="disabled"):
        real_cloud_reader(cfg, max_tokens=100)
    cfg.workflows.rule_reader_cloud = True
    with pytest.raises(rule_reader.ReaderUnavailable, match="approved"):
        real_cloud_reader(cfg, max_tokens=100)
