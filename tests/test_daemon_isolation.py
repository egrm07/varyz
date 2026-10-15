"""The native launcher's isolated Python must also isolate its daemon child."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path


def test_isolated_cli_starts_daemon_with_foreign_python_environment(tmp_path: Path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ndata_dir="data"\n[resolution]\nport={port}\n')
    # The parent is explicitly isolated, as the native launcher is. A normal
    # child loses -I and the deliberately invalid PYTHONHOME prevents startup.
    probe = """
import json, sys, time
from pathlib import Path
from varyz.config import load_config
from varyz.db.connection import open_db
from varyz.resolution.client import ResolutionClient, ResolutionClientError, ensure_daemon
assert sys.flags.isolated
path = Path(sys.argv[1])
cfg = load_config(path)
cfg.data_dir.mkdir(parents=True, exist_ok=True)
open_db(cfg.db_path).close()
client = ResolutionClient(cfg.resolution.bind_host, cfg.resolution.port,
                          archive=cfg.db_path, config_path=path, timeout=1)
status = ensure_daemon(client, path, timeout=8)
assert status is not None, 'isolated parent could not start its daemon child'
try:
    assert client.status() is not None
finally:
    client.stop()
    for _ in range(50):
        try:
            client.status()
        except ResolutionClientError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError('synthetic daemon did not stop')
print(json.dumps({'daemon_started': True, 'daemon_stopped': True}))
"""
    env = {
        **os.environ,
        "PYTHONHOME": str(tmp_path / "nonexistent-foreign-python"),
        "PYTHONPATH": str(tmp_path / "foreign-modules"),
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(config)], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"daemon_started": True, "daemon_stopped": True}
