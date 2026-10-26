"""Bounded local preference reading for setup's More privacy mode.

The worker uses the existing quoted-evidence reader and candidate queue. A
cross-process lock prevents overlap; pause and mode changes stop the next call.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from varyz.config import load_config
from varyz.db.connection import FileLock, open_db

COOLDOWN_SECONDS = 300
MAX_CALLS = 30
# How long a windowless reader waits for setup's model before it gives up:
# without a limit, a download that never finished left it running for good.
MODEL_WAIT_SECONDS = 3 * 60 * 60.0


def _allowed(cfg: Any) -> bool:
    from varyz.autosync import is_paused
    from varyz.hooks import pause_path

    return (cfg.workflows.inference_mode == "local" and cfg.workflows.rule_miner == "model"
            and cfg.connector_options("ai_conversations").get("approved", False)
            and not is_paused(cfg) and not pause_path(cfg).exists())


def load_status(cfg: Any) -> dict[str, Any]:
    try:
        value = json.loads((Path(cfg.data_dir) / "rules" / "local_reader.json").read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def work_left(cfg: Any) -> tuple[int, int]:
    from varyz.resolution.profile_reader import ProfileState, profile_path, waiting

    state = ProfileState(profile_path(cfg, "local"))
    if not cfg.db_path.is_file():
        return 0, len(state.dirty)
    conn = open_db(cfg.db_path)
    try:
        return waiting(state, conn, "local"), len(state.dirty)
    finally:
        conn.close()


def _stop_path(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "rules" / "local_reader.stop"


def stop(cfg: Any) -> None:
    """Ask a running reader to stop before its next model call; what it read is kept."""
    path = _stop_path(cfg)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")


def _stopped(cfg: Any) -> bool:
    return _stop_path(cfg).exists()


def model_ready(cfg: Any) -> bool:
    import httpx

    try:
        response = httpx.get(cfg.workflows.classifier_url.rstrip("/") + "/api/tags", timeout=3)
        response.raise_for_status()
        wanted = cfg.workflows.classifier_model
        return any(item.get("name") in {wanted, wanted + ":latest"}
                   for item in response.json().get("models", []))
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False


def request(cfg: Any, config_file: Path, *, wait_model: bool = False,
            force: bool = False) -> bool:
    """Start a reader; ``force`` is the owner's own retry, which skips the cooldown."""
    if not _allowed(cfg):
        return False
    try:
        last = load_status(cfg)
        if not force and time.time() - float(last.get("started_at", 0)) < COOLDOWN_SECONDS:
            return False
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    # A new start (the owner's retry, or their next prompt) lifts an earlier stop.
    with contextlib.suppress(OSError):
        _stop_path(cfg).unlink(missing_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            [sys.executable, "-m", __name__, str(config_file.resolve())]
            + (["--wait-model"] if wait_model else []),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, start_new_session=sys.platform != "win32",
        )
    except OSError:
        return False
    return True


def run(config_file: Path, *, wait_model: bool = False) -> None:
    from varyz import cli
    from varyz.resolution.profile_reader import (
        LOCAL_REPLY_TOKENS,
        SCHEMA,
        SOURCES,
        ProfileState,
        known_from,
        profile_path,
        queue_items,
        read_with_model,
    )
    from varyz.resolution.rule_reader import ReaderUnavailable, reader_from_config

    cfg = load_config(config_file)
    if not _allowed(cfg) or not cfg.db_path.is_file():
        return
    folder = Path(cfg.data_dir) / "rules"
    folder.mkdir(parents=True, exist_ok=True)
    lock = FileLock(folder / "local_reader.lock")
    if not lock.acquire():
        return
    status = {"started_at": time.time(), "state": "waiting_model" if wait_model else "running"}
    path = folder / "local_reader.json"
    conn = None
    try:
        path.write_text(json.dumps(status), encoding="utf-8")
        waiting_since = time.monotonic()
        while wait_model and not model_ready(cfg):
            if time.monotonic() - waiting_since > MODEL_WAIT_SECONDS:
                status.update(state="stopped",
                              reason="The local model did not arrive; run varyz setup again "
                                     "to finish it and start learning.")
                return
            time.sleep(5)
            try:
                cfg = load_config(config_file)
            except (OSError, ValueError):  # setup may be rewriting it; read it next time
                continue
            if not _allowed(cfg):
                status.update(state="stopped",
                              reason="Local learning was paused or the AI mode changed.")
                return
            if _stopped(cfg):
                status.update(state="stopped", reason="asked")
                return
        status.update(state="running", reading_at=time.time())
        path.write_text(json.dumps(status), encoding="utf-8")
        provider = reader_from_config(cfg, schema=SCHEMA, max_tokens=LOCAL_REPLY_TOKENS)

        class Guarded:
            def complete(self, system: str, user: str) -> str:
                if not _allowed(load_config(config_file)):
                    raise ReaderUnavailable("local reading was paused or the AI mode changed")
                if _stopped(cfg):
                    raise ReaderUnavailable("asked")
                began = time.monotonic()
                reply = provider.complete(system, user)
                # How long one call takes, so setup can time the merges still to come.
                took = time.monotonic() - began
                last = status.get("call_seconds")
                status["call_seconds"] = round(
                    0.7 * last + 0.3 * took if isinstance(last, float) else took, 2)
                with contextlib.suppress(OSError):
                    path.write_text(json.dumps(status), encoding="utf-8")
                return reply

        client = cli._resolution_client(cfg)
        caller = cli._local_owner_caller(cfg, "owner_knowledge")
        conn = open_db(cfg.db_path)
        state = ProfileState(profile_path(cfg, "local"))
        known = known_from((*client.principles("confirmed"), *client.principles("candidate")),
                           client.knowledge())
        def add(**fields: Any) -> str | None:
            return client.add_principle(caller=caller,
                                        device_id=cfg.resolution.device_id, **fields)

        while True:
            before = work_left(cfg)
            items = read_with_model(state, conn, Guarded(), tier="local", reader="local",
                                    known=known, max_calls=MAX_CALLS)
            if items:
                queue_items(items, add, SOURCES["local"])
            state.save()
            after = work_left(cfg)
            if after == (0, 0):
                break
            if after == before:
                raise ReaderUnavailable("The local model made no progress; retry learning later.")
        status["state"] = "idle"
    except ReaderUnavailable as exc:
        # Varyz's own sentences say why; capped, as one may quote a connection error.
        status.update(state="stopped", reason=(str(exc) or type(exc).__name__)[:200])
    except Exception as exc:
        # No prompt, excerpt, model response or credential enters this status file.
        status.update(state="stopped", reason=type(exc).__name__)
    finally:
        if conn is not None:
            conn.close()
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(status), encoding="utf-8")
        lock.release()


if __name__ == "__main__":
    run(Path(sys.argv[1]), wait_model="--wait-model" in sys.argv[2:])
