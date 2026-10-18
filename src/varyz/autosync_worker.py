"""Console-free entrypoint for detached one-shot refreshes."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from varyz.autosync import run_once


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Varyz background refresh")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--trigger", default="scheduled")
    parser.add_argument("--claimed", action="store_true")
    args = parser.parse_args(argv)
    result = run_once(
        args.config.resolve(),
        trigger=args.trigger,
        claimed=args.claimed,
    )
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
