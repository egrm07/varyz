#!/usr/bin/env bash
# One compile, semantic mode, logged. Written as a file because the flags have
# to survive being backgrounded intact: --run is one word away from a dry run.
set -o pipefail
cd "$(dirname "$0")/.."
stamp="$(date -u +%Y%m%d-%H%M%S)"
log="reports/compile-${stamp}.log"
echo "started $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$log"
uv run --no-sync varyz model compile --run --semantic >> "$log" 2>&1
code=$?
echo "exit ${code} at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$log"
exit "$code"
