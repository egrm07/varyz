"""Machine selection, download avoidance, and conversation-only consent boundaries."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from tests.test_setup_web import _finished, machine  # noqa: F401
from varyz import cli
from varyz.config import Config, load_config
from varyz.configfile import set_inference_mode
from varyz.resolution import headless_reader, local_reader, rule_reader
from varyz.resolution.local_classifier import ClassifierUnavailable, local_model_from_config
from varyz.resolution.rule_reader import cloud_reader_from_config as real_cloud_reader
from varyz.setup_machine import GIB, Machine, assess, memory, plan
from varyz.setup_web import SetupSession

MAC = Machine("Darwin", "arm64", 8, 16 * GIB, 8 * GIB, 40 * GIB)
runner = CliRunner()


@pytest.mark.parametrize(("hardware", "comfortable"), [
    (MAC, True),
    (replace(MAC, total_memory=8 * GIB), False),
    (replace(MAC, available_memory=3 * GIB), False),
    (replace(MAC, available_memory=7 * GIB), False),
    (replace(MAC, free_disk=11 * GIB), False),
    (replace(MAC, cores=2), False),
    (replace(MAC, architecture="x86_64"), False),
    (replace(MAC, total_memory=None), False),
    (replace(MAC, available_memory=None), False),
    (replace(MAC, free_disk=None), False),
    (Machine("Windows", "amd64", 8, 32 * GIB, 12 * GIB, 40 * GIB, 1000, 12000), True),
    (Machine("Linux", "x86_64", 8, 32 * GIB, 12 * GIB, 40 * GIB, 6000, 12000), False),
    (Machine("Windows", "amd64", 8, 32 * GIB, 12 * GIB, 40 * GIB), False),
])
def test_comfort_requires_headroom_for_apps(hardware, comfortable):
    result = assess(hardware)
    assert result.comfortable is comfortable
    assert bool(result.reasons) is not comfortable


def test_mac_memory_probe_uses_reclaimable_pages(monkeypatch):
    def output(args):
        return str(16 * GIB) if args[0] == "sysctl" else (
            'Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
            'Pages free: 100.\nPages inactive: 200.\nPages speculative: 50.\n'
            'Pages wired down: 999.\n'
        )
    monkeypatch.setattr("varyz.setup_machine._output", output)
    assert memory("Darwin") == (16 * GIB, 350 * 16384)


def test_unknown_probe_never_assumes_capacity(monkeypatch):
    monkeypatch.setattr("varyz.setup_machine._output", lambda _args: "unrecognized")
    assert memory("Darwin") == (None, None)


def test_local_memory_reason_reports_current_headroom():
    result = assess(replace(MAC, available_memory=128 * 1024**2))
    assert result.comfortable is False
    assert result.reasons == (
        "Only 0.1 GB of memory is available now. Local learning needs at least 8 GB free.",
    )


def test_small_mac_auto_selects_conversations_without_granting_cloud(tmp_path, monkeypatch):
    monkeypatch.setattr("varyz.setup_machine.inspect_machine",
                        lambda _path: replace(MAC, total_memory=8 * GIB))
    target = tmp_path / "config.toml"
    view = plan(target)
    assert view["selected"] == "conversations"
    assert view["choices"] == ["conversations"]
    assert view["cloud_conversations"] is False
    assert not target.exists()


def test_saved_choice_and_consent_survive_rerun(tmp_path, monkeypatch):
    monkeypatch.setattr("varyz.setup_machine.inspect_machine", lambda _path: MAC)
    target = tmp_path / "config.toml"
    target.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    set_inference_mode(target, "conversations", cloud_conversations=True)
    assert plan(target)["selected"] == "conversations"
    assert plan(target)["cloud_conversations"] is True
    before = target.read_bytes()
    set_inference_mode(target, "conversations", cloud_conversations=True)
    assert target.read_bytes() == before


def test_selection_preserves_other_config_and_revokes_cloud_in_local_mode(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text('[connectors.documents]\napproved = true\n# keep me\n'
                      '[workflows]\nclassifier_model = "custom"\n', encoding="utf-8")
    set_inference_mode(target, "conversations", cloud_conversations=True)
    assert load_config(target).workflows.task_classifier == "embedding"
    set_inference_mode(target, "local", cloud_conversations=True)
    cfg = load_config(target)
    assert not cfg.workflows.rule_reader_cloud
    assert not cfg.workflows.rule_reader_agent
    assert cfg.workflows.classifier_model == "custom"
    assert cfg.connector_options("documents")["approved"]
    assert "# keep me" in target.read_text(encoding="utf-8")


def test_conversation_mode_never_probes_or_starts_ollama(monkeypatch):
    cfg = Config()
    cfg.workflows.inference_mode = "conversations"
    monkeypatch.setattr("varyz.resolution.local_classifier.socket.create_connection",
                        lambda *a, **kw: pytest.fail("must not touch Ollama"))
    model = local_model_from_config(cfg)
    with pytest.raises(ClassifierUnavailable, match="disabled"):
        model.ready()
    with pytest.raises(ClassifierUnavailable, match="disabled"):
        model.size_bytes()
    assert rule_reader.reading_order(cfg, ("fits", "")) == ["cloud"]
    cfg.workflows.rule_reader_cloud = False
    assert rule_reader.reading_order(cfg, ("fits", "")) == []


def test_local_mode_never_builds_cloud_reader_or_runs_cloud_agent(monkeypatch):
    cfg = Config()
    cfg.workflows.inference_mode = "local"
    monkeypatch.setattr("varyz.llm.providers.build_provider",
                        lambda *a, **kw: pytest.fail("must not build cloud provider"))
    assert rule_reader.reading_order(cfg, ("fits", "")) == ["local"]
    assert headless_reader.enabled(cfg, "full") is False
    with pytest.raises(rule_reader.ReaderUnavailable, match="disabled"):
        real_cloud_reader(cfg, max_tokens=100)


def test_small_machine_setup_never_downloads_model(machine, monkeypatch):  # noqa: F811
    monkeypatch.setattr("varyz.setup_machine.inspect_machine",
                        lambda _path: replace(MAC, total_memory=8 * GIB))
    monkeypatch.setattr(cli, "_ready_classifier",
                        lambda *a, **kw: pytest.fail("must not install Ollama"))
    session = SetupSession(machine.config)
    assert session.start(["claude-code"], ["ai_conversations"],
                         cloud_conversations=True) is None
    result = _finished(session)["result"]
    assert result["verdict"] == "GO"
    cfg = load_config(machine.config)
    assert cfg.workflows.inference_mode == "conversations"
    assert cfg.workflows.rule_reader_cloud
    assert "no Ollama" in session.steps[6].detail


def test_web_cannot_force_local_model_on_small_machine(machine, monkeypatch):  # noqa: F811
    monkeypatch.setattr("varyz.setup_machine.inspect_machine",
                        lambda _path: replace(MAC, total_memory=8 * GIB))
    session = SetupSession(machine.config)
    assert "cannot comfortably" in session.start([], [], inference_mode="local")
    assert not session.started
    assert not machine.config.exists()


def test_yes_on_small_machine_does_not_approve_cloud(machine, monkeypatch):  # noqa: F811
    monkeypatch.setattr("varyz.setup_machine.inspect_machine",
                        lambda _path: replace(MAC, total_memory=8 * GIB))
    result = runner.invoke(cli.app, ["setup", "--yes", "--config", str(machine.config)])
    assert result.exit_code == 0, result.output
    cfg = load_config(machine.config)
    assert cfg.workflows.inference_mode == "conversations"
    assert not cfg.workflows.rule_reader_cloud
    assert not cfg.workflows.rule_reader_agent


def test_local_background_reader_requires_local_mode_and_selected_chats(tmp_path):
    cfg = Config(base_dir=tmp_path)
    cfg.workflows.inference_mode = "conversations"
    assert local_reader._allowed(cfg) is False
    cfg.workflows.inference_mode = "local"
    assert local_reader._allowed(cfg) is False
    cfg.connectors["ai_conversations"] = {"approved": True}
    assert local_reader._allowed(cfg)


def test_cloud_turns_exclude_documents_sensitive_and_unclassified(conn):
    from tests.test_profile_reader import _entry
    from varyz.resolution.rule_miner import MiningStats

    text = "Always keep the launch notes concise and use local fonts in every project."
    _entry(conn, "allowed", text, sensitivity="personal")
    _entry(conn, "private", text, sensitivity="sensitive")
    _entry(conn, "unknown", text, sensitivity="unclassified")
    _entry(conn, "document", "PROJECT FILE CONTENT: " + text, sensitivity="personal")
    conn.execute("UPDATE entries SET source = 'documents' WHERE id = 'document'")
    conn.commit()
    turns = rule_reader.owner_turns(conn, MiningStats(), cloud=True)
    assert [turn.entry_id for turn in turns] == ["allowed"]


def test_local_background_worker_queues_candidates_without_cloud(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text('[storage]\ndata_dir = "data"\n'
                      '[connectors.ai_conversations]\napproved = true\n', encoding="utf-8")
    set_inference_mode(target, "local", cloud_conversations=False)
    cfg = load_config(target)
    db = local_reader.open_db(cfg.db_path)
    db.close()
    queued = []
    client = SimpleNamespace(principles=lambda _status: (), knowledge=lambda: (),
                             add_principle=lambda **fields: queued.append(fields) or "candidate")
    monkeypatch.setattr(cli, "_resolution_client", lambda _cfg: client)
    monkeypatch.setattr(rule_reader, "reader_from_config", lambda *a, **kw:
                        SimpleNamespace(complete=lambda system, user: "[]"))

    def read(state, conn, provider, **kwargs):
        assert kwargs["tier"] == "local"
        assert kwargs["max_calls"] == 30
        assert provider.complete("instructions", "local excerpt") == "[]"
        return [SimpleNamespace(principle=lambda source:
                                {"statement": "Use local fonts", "provenance": {},
                                 "source": source})]

    monkeypatch.setattr("varyz.resolution.profile_reader.read_with_model", read)
    local_reader.run(target)
    import json

    status = json.loads((cfg.data_dir / "rules" / "local_reader.json").read_text())
    assert status["state"] == "idle", status
    assert [item["statement"] for item in queued] == ["Use local fonts"]
    assert queued[0]["status"] == "candidate"


def test_apple_silicon_can_use_local_evidence_judge(monkeypatch):
    from varyz.retrieval import judge

    cfg = Config()
    cfg.workflows.inference_mode = "local"
    monkeypatch.setattr(judge.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(judge.platform, "machine", lambda: "arm64")
    monkeypatch.setattr("varyz.resolution.local_classifier.gpu_memory_mib", lambda: None)
    monkeypatch.setattr(judge, "_ClassifierModel", lambda _cfg:
                        SimpleNamespace(available=lambda: True))
    assert judge._classifier_judge(cfg) is not None


def test_cloud_permission_change_stops_an_existing_reader(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text('[connectors.ai_conversations]\napproved = true\n', encoding="utf-8")
    set_inference_mode(target, "conversations", cloud_conversations=True)
    calls = []
    model = SimpleNamespace(complete=lambda *args: calls.append(args) or "[]")
    guard = rule_reader.ConversationGuard(load_config(target), model)
    assert guard.complete("instructions", "eligible chat") == "[]"
    set_inference_mode(target, "local", cloud_conversations=False)
    with pytest.raises(rule_reader.ReaderUnavailable, match="disabled"):
        guard.complete("instructions", "must stay local")
    assert calls == [("instructions", "eligible chat")]


def test_long_lived_mcp_reader_sees_permission_revocation(tmp_path):
    from varyz.mcp_server import VaryzServer

    target = tmp_path / "config.toml"
    target.write_text('[connectors.ai_conversations]\napproved = true\n', encoding="utf-8")
    set_inference_mode(target, "conversations", cloud_conversations=True)
    caller = SimpleNamespace(capability_id="registered-reader")
    server = SimpleNamespace(cfg=load_config(target), _advice_caller=lambda _args: (caller, None))
    assert VaryzServer._reading_caller(server, {}) == (caller, None)
    set_inference_mode(target, "local", cloud_conversations=False)
    _, denial = VaryzServer._reading_caller(server, {})
    assert "disabled" in denial


def test_mode_and_consent_are_published_in_one_write(tmp_path, monkeypatch):
    from varyz import configfile

    target = tmp_path / "config.toml"
    target.write_text("# original settings\n", encoding="utf-8")
    writes = []
    original = configfile._write

    def write(path, text):
        original(path, text)
        writes.append(load_config(path).workflows)

    monkeypatch.setattr(configfile, "_write", write)
    set_inference_mode(target, "conversations", cloud_conversations=False)
    assert len(writes) == 1
    assert writes[0].inference_mode == "conversations"
    assert not writes[0].rule_reader_cloud
