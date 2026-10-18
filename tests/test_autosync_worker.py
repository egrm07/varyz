"""The scheduled entrypoint never imports the human CLI or writes to stdout."""

from types import SimpleNamespace

from varyz import autosync_worker


def test_worker_passes_absolute_config_and_machine_status(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("[storage]\ndata_dir = \"data\"\n", encoding="utf-8")
    calls = []

    def fake_run(config_path, **kwargs):
        calls.append((config_path, kwargs))
        return SimpleNamespace(status="ok")

    monkeypatch.setattr(autosync_worker, "run_once", fake_run)
    code = autosync_worker.main(
        ["--config", str(config), "--trigger", "scheduled", "--claimed"]
    )
    assert code == 0
    assert calls == [
        (config.resolve(), {"trigger": "scheduled", "claimed": True})
    ]


def test_worker_returns_failure_status(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    monkeypatch.setattr(
        autosync_worker,
        "run_once",
        lambda *_args, **_kwargs: SimpleNamespace(status="failed"),
    )
    assert autosync_worker.main(["--config", str(config)]) == 1
