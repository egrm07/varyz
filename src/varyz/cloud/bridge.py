"""Start the loopback Cloud bridge for the setup page, including older daemon ports.

Readiness is local and account-free. Discovery never signs in, selects data or
starts a sync worker; the Cloud page retains its separate approval flow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from varyz.cloud.computer import (
    BRIDGE_PATH,
    CLOUD_ORIGIN,
    ComputerConnection,
    handle_computer_request,
)

PORT = 1207
CLOUD_SETUP_URL = CLOUD_ORIGIN + "/?view=setup#connect-varyz"


def ready(config: Path, port: int = PORT) -> bool:
    request = Request(f"http://127.0.0.1:{port}{BRIDGE_PATH}/identity",
                      headers={"Origin": CLOUD_ORIGIN})
    try:
        with urlopen(request, timeout=1) as response:
            identity = json.loads(response.read(1024))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return False
    expected = hashlib.sha256(str(config.resolve()).encode()).hexdigest()
    if identity.get("config_hash") != expected:
        raise ValueError("Cloud is connected to a different local archive. Keep its setup open.")
    return True


def ensure_ready(config: Path, *, port: int = PORT, timeout: float = 10) -> str:
    """Reuse this archive's bridge or start one detached; never take another archive's port."""
    config = config.resolve()
    if not config.is_file():
        raise ValueError("Finish local Varyz setup first.")
    if ready(config, port):
        return CLOUD_SETUP_URL
    # Newer native builds already serve the bridge on their resolution port.
    from varyz.cli import _resolution_client, _restart_daemon
    from varyz.config import load_config
    from varyz.resolution.client import ensure_daemon

    cfg = load_config(config)
    if cfg.resolution.port == port:
        client = _resolution_client(cfg)
        status = ensure_daemon(client, config)
        if status is not None and not ready(config, port):
            _restart_daemon(config)
        if ready(config, port):
            return CLOUD_SETUP_URL
    # Older installs use 8765 for resolution; Cloud discovers 1207. Keep those
    # existing agent settings intact and provide only the browser bridge here.
    command = [sys.executable, "-m", "varyz.cloud.bridge", "--config", str(config),
               "--port", str(port)]
    if sys.flags.isolated:
        command.insert(1, "-I")
    # Detached like the daemon: closing the setup page that opened it must not stop it.
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
                               start_new_session=sys.platform != "win32")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready(config, port):
            return CLOUD_SETUP_URL
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise ValueError("Local Varyz could not open its Cloud connection. Please try again.")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: Path, port: int = PORT) -> None:
        self.computer_connection: ComputerConnection | None = ComputerConnection(config)
        self.last_request = time.monotonic()
        super().__init__(("127.0.0.1", port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: BridgeServer

    def _handle(self) -> None:
        if handle_computer_request(self, self.server.computer_connection):
            self.server.last_request = time.monotonic()
            return
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_OPTIONS = _handle

    def log_message(self, *_args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    with BridgeServer(args.config, args.port) as server:
        connection = server.computer_connection
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            while (time.monotonic() - server.last_request < 1800
                   or (connection is not None and connection.active())):
                time.sleep(1)
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
