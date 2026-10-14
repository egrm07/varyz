"""Check deployment surgery without contacting or changing a live server."""

from pathlib import Path

from deploy.cloud.deploy_analytics import FILES, REMOTE_SCRIPT


def test_rollout_only_changes_analytics_registration_and_preserves_other_code(tmp_path):
    root = tmp_path / "deployed"
    (root / "src/varyz/cloud").mkdir(parents=True)
    (root / "deploy/cloud").mkdir(parents=True)
    existing = """# An unrelated deployed feature must survive.
class Settings:
    paddle_sandbox = True
    @classmethod
    def from_env(cls):
        return cls(
        )
def factory():
    app.state.settings = settings
    return app
"""
    (root / "src/varyz/cloud/app.py").write_text(existing)
    (root / "deploy/cloud/compose.yaml").write_text("services:\n  cloud:\n    environment:\n")
    env = tmp_path / "cloud.env"
    env.write_text("VARYZ_PADDLE_SANDBOX=true\n")
    prefix = REMOTE_SCRIPT.split("folder = pathlib.Path", 1)[0]
    prefix = prefix.replace("FILE_DATA", "{}", 1)
    prefix = prefix.replace("'/srv/varyz/src'", repr(str(root)))
    prefix = prefix.replace("'/etc/varyz-cloud/cloud.env'", repr(str(env)))
    state = {}
    exec(compile(prefix, "rollout-plan", "exec"), state)
    assert state["app_text"].count("register_analytics(app, settings, store)") == 1
    assert "paddle_sandbox = True" in state["app_text"]
    assert "unrelated deployed feature" in state["app_text"]
    assert state["compose_text"].count("      VARYZ_ANALYTICS_ENABLED:") == 1
    assert (root / "src/varyz/cloud/app.py").read_text() == existing
    assert all("analytics" in name for name in FILES)
    assert "src/varyz/analytics.py" not in FILES


def test_rollout_remote_script_compiles_with_real_shared_schema():
    root = Path(__file__).resolve().parents[1]
    files = {name: (root / name).read_text(encoding="utf-8") for name in FILES}
    compile(REMOTE_SCRIPT.replace("FILE_DATA", repr(files), 1), "rollout", "exec")


def test_normal_rollout_keeps_enabled_analytics_credentials(monkeypatch, capsys):
    from deploy.cloud import deploy

    monkeypatch.setattr(
        deploy.keyring,
        "get_password",
        lambda service, name: (
            "synthetic-owner-token" if name == "VARYZ_ANALYTICS_ADMIN_TOKEN" else None
        ),
    )
    environment = deploy.environment(False)
    assert "VARYZ_ANALYTICS_ENABLED=true\n" in environment
    assert "VARYZ_ANALYTICS_ADMIN_TOKEN=synthetic-owner-token\n" in environment
    assert "VARYZ_PADDLE_SANDBOX=true\n" in environment
    assert "synthetic-owner-token" not in capsys.readouterr().out
