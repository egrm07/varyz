"""`python -m varyz` — the fallback launcher when the console script is not on
PATH, which is the normal case for a checkout and for MCP clients that start
servers with a bare interpreter."""

from varyz.cli import app

if __name__ == "__main__":
    app()
