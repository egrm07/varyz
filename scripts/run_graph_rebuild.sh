#!/usr/bin/env bash
# One graph rebuild, logged, with restarts. Written as a file for the same
# reason `run_compile.sh` is: the flags have to survive being backgrounded.
#
# A rebuild is resumable -- it checkpoints per entry and reuses every cached
# extraction whose reader and text are unchanged -- so a restart costs the
# entry it died on and nothing else. Free physical memory is recorded at both
# ends of every attempt, because a run that kills its model server rather than
# stopping it leaves driver-pinned host pages behind, and the only way to see
# that is to look before and after.
set -o pipefail
cd "$(dirname "$0")/.."
attempts="${ATTEMPTS:-4}"
stamp="$(date -u +%Y%m%d-%H%M%S)"
log="reports/graph-rebuild-${stamp}.log"

memory() {
  uv run --no-sync python -c "
from varyz.llm.local_runtime import host_memory
m = host_memory()
print('memory unreadable' if m is None else f'free {m.free_mib} MiB of {m.total_mib}')
" 2>/dev/null || echo "memory unreadable"
}

# A rebuild does not hold the write lock for its whole run -- it checkpoints
# per entry and lets go in between -- so a scheduled sync can take the lock in
# a gap, and the rebuild's next checkpoint finds it held and exits. That is a
# wait, not a failure: retrying instantly spent all four attempts of the
# 2026-09-18 run in five seconds, sixteen minutes of extraction in. Losing the
# lock costs a wait; only a real error costs an attempt.
lock_wait="${LOCK_WAIT:-180}"
lock_waits="${LOCK_WAITS:-40}"

echo "started $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$log"
code=1
attempt=0
waited=0
while [ "$attempt" -lt "$attempts" ]; do
  attempt=$((attempt + 1))
  echo "--- attempt ${attempt}/${attempts} $(date -u +%Y-%m-%dT%H:%M:%SZ) --- $(memory)" >> "$log"
  # Held awake for the run: the 2026-09-18 v9 attempt was thirty-three
  # minutes in when Windows slept on an idle timer and took every process
  # in the tree with it. The hold dies with this command, so the machine
  # goes back to sleeping normally the moment the rebuild is over.
  uv run --no-sync python scripts/keep_awake.py uv run --no-sync varyz graph rebuild >> "$log" 2>&1
  code=$?
  echo "--- attempt ${attempt} exit ${code} $(date -u +%Y-%m-%dT%H:%M:%SZ) --- $(memory)" >> "$log"
  [ "$code" -eq 0 ] && break
  if tail -n 5 "$log" | grep -q "Another varyz process is writing"      && [ "$waited" -lt "$lock_waits" ]; then
    waited=$((waited + 1))
    attempt=$((attempt - 1))
    echo "--- lock held; wait ${waited}/${lock_waits} of ${lock_wait}s, attempt not spent ---" >> "$log"
    sleep "$lock_wait"
  fi
done
echo "exit ${code} at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$log"
exit "$code"
