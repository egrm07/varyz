"""Render actual setup UI with a synthetic archive; never start setup processes."""

import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

from varyz.bootstrap import DetectedSources
from varyz.setup_web import SetupServer, SetupSession

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "analytics-preview"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = OUT / "config.toml"
    cfg.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")
    with (
        patch("varyz.install.client_paths", return_value={}),
        patch("varyz.setup_web._gh_token", return_value=None),
    ):
        session = SetupSession(cfg, detected=DetectedSources((), (), ()))
    server = SetupServer(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
        result = subprocess.run(
            [
                str(runtime / "bin/node.exe"),
                str(ROOT / "scripts/analytics_preview.cjs"),
                server.url,
                str(OUT),
                str(runtime / "node_modules/playwright"),
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        print(result.stdout.decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
