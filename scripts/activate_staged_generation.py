"""Activate a generation whose compile finished every stage but the pointer swap.

`PersonalModelCompiler._activate_generation` calls `_assert_inputs_current`
before it swaps the pointer, and that call re-reads, re-sorts and re-hashes every
entry in the archive.  It is cheap at the start of a run and ruinous at the end
of one: on 2026-09-08 the compiler wrote `activate: complete` at 02:16:18Z and
then spent three hours inside that check, in a process whose heap had grown to
15 GB holding the generation it had just staged.  Windows trimmed its working
set to a few hundred megabytes, and it page-faulted at roughly 9,600 reads a
second without ever reaching the swap.  The comment above that checkpoint
already describes the state this leaves behind -- "a run killed in between
leaves ``activate: complete`` above a generation still at ``staged``" -- and
this script is how that state is resolved without recompiling.

What `_assert_inputs_current` establishes is that the archive did not change
while the compile ran.  That is checked here directly instead of by rebuilding
the hash: the generation records the graph run it was pinned to, and a graph run
records the content hash and entry count of the evidence projection beneath it.
If the active graph is still that graph, still carries that hash, and the entry
count still matches, then the inputs the compiler hashed are the inputs on disk,
and the reconstruction adds nothing but hours.

Nothing else about the generation is touched.  Its records were staged,
validated and readiness-scored by the compiler itself; the repository re-checks
their integrity inside the swap's own transaction.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from varyz.config import load_config
from varyz.db.connection import WriteLock, open_db
from varyz.personal_model.repository import PersonalModelRepository


def _refused(message: str) -> int:
    print(f"refused: {message}")
    return 1


def activate(conn: sqlite3.Connection, data_dir: Path, generation_id: str) -> int:
    generation = conn.execute(
        """SELECT graph_generation_id, state FROM personal_model_generations WHERE id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:
        return _refused(f"no generation {generation_id}")
    if generation["state"] != "staged":
        return _refused(f"generation is {generation['state']}, not staged")

    pinned = conn.execute(
        """SELECT id, input_content_hash, entries_total, state
             FROM graph_runs WHERE id=?""",
        (generation["graph_generation_id"],),
    ).fetchone()
    active = conn.execute(
        """SELECT id, input_content_hash, entries_total FROM graph_runs
            WHERE state='active' ORDER BY activated_at DESC LIMIT 1"""
    ).fetchone()
    if pinned is None:
        pinned_id = generation["graph_generation_id"]
        return _refused(f"the graph run it was pinned to is gone: {pinned_id}")
    if active is None or active["id"] != pinned["id"]:
        return _refused(
            f"the active graph is {active and active['id']}, not the pinned {pinned['id']}"
        )
    if active["input_content_hash"] != pinned["input_content_hash"]:
        return _refused("the pinned graph's input hash changed during the compile")

    entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    expected = pinned["entries_total"]
    if expected is not None and entries != expected:
        return _refused(f"{entries} entries now, {expected} beneath the pinned graph")

    repository = PersonalModelRepository(conn)
    print(f"inputs unchanged ({entries} entries under graph {pinned['id']})")
    print(f"active generation before: {repository.active_generation_id()}")

    with WriteLock(data_dir):
        activated = repository.activate_generation(generation_id)

    print(
        f"state={activated.state} records={activated.record_count} "
        f"reused={activated.reused_record_count} quarantined={activated.quarantine_count}"
    )
    print(f"active generation now: {repository.active_generation_id()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generation_id", help="the staged generation to activate")
    parser.add_argument("--config", type=Path, default=None)
    arguments = parser.parse_args(argv)

    config = load_config(arguments.config)
    connection = open_db(config.db_path)
    connection.row_factory = sqlite3.Row
    return activate(connection, config.db_path.parent, arguments.generation_id)


if __name__ == "__main__":
    sys.exit(main())
