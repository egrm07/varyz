"""Compatibility entry point for the setup Consent preview and interaction checks."""

import subprocess
from pathlib import Path

from consent_preview import OUT, ROOT, main

if __name__ == "__main__":
    main()
    runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    subprocess.run([str(runtime / "bin/node.exe"), str(ROOT / "scripts/consent_preview.cjs"),
                    str(OUT), str(runtime / "node_modules/playwright")], check=True)
