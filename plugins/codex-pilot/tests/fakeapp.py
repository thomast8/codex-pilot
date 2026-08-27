"""A stand-in Codex Desktop router on a real AF_UNIX socket.

Real socket, real framing, real accept loop. A reconnect test has to prove what
the *app* was told after the socket was replaced, and only a second accepted
connection with its own received-frame log can show that.

AF_UNIX paths cap at ~104 bytes on macOS and pytest's tmp_path already exceeds
that, so callers bind under a short `/tmp` directory.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codex_pilot.framing import FrameReader, encode_frame

FOLLOWING_CHANGED = "thread-stream-following-changed"


def wait_until(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    """Poll to a deadline. The pump ticks on its own schedule, so sleeps lie."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def write_index(home: Path, thread_id: str, name: str = "watched") -> None:
    """Enough session index for ThreadStore.resolve to bind a bare uuid."""
    home.mkdir(parents=True, exist_ok=True)
    row = {"id": thread_id, "thread_name": name, "updated_at": "2026-01-01T00:00:00Z"}
    (home / "session_index.jsonl").write_text(json.dumps(row) + "\n")


class Connection:
    """One accepted client, with its own log of what it was sent."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.reader = FrameReader()
        self._received: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, msg: dict[str, Any]) -> None:
        with self._lock:
            self._received.append(msg)

    @property
    def received(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._received)

    def followed_threads(self) -> list[str]:
        """Threads this connection was asked to follow, in order."""
        out: list[str] = []
        for msg in self.received:
            if msg.get("method") != FOLLOWING_CHANGED:
                continue
            params = msg.get("params") or {}
            if params.get("following") is True:
                out.append(str(params.get("conversationId")))
        return out

    def send(self, message: dict[str, Any]) -> None:
        self.sock.sendall(encode_frame(message))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()


class FakeApp:
    """Serves `$CODEX_HOME/ipc/ipc.sock` the way Codex Desktop does."""

    def __init__(self, home: Path, answer_initialize: bool = True) -> None:
        # answer_initialize=False accepts the connection and then says nothing,
        # which is how a wedged app looks from the client side: the handshake
        # hangs to the full IPC timeout rather than failing.
        self.answer_initialize = answer_initialize
        (home / "ipc").mkdir(parents=True, exist_ok=True)
        self.path = home / "ipc" / "ipc.sock"
        # A unix socket outlives its server as a file on disk, so restarting
        # over the same path means unlinking first -- exactly what the real app
        # does when it comes back up.
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(8)
        self._connections: list[Connection] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def connections(self) -> list[Connection]:
        with self._lock:
            return list(self._connections)

    def wait_for_connection(self, count: int, timeout: float = 15.0) -> Connection:
        assert wait_until(lambda: len(self.connections) >= count, timeout), (
            f"expected {count} connection(s), saw {len(self.connections)}"
        )
        return self.connections[count - 1]

    def replay(self, frames: list[dict[str, Any]], conn: Connection) -> None:
        for frame in frames:
            conn.send(frame)

    def close(self) -> None:
        self._stop.set()
        for conn in self.connections:
            conn.close()
        with contextlib.suppress(OSError):
            self.server.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            conn = Connection(sock)
            with self._lock:
                self._connections.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: Connection) -> None:
        while not self._stop.is_set():
            try:
                chunk = conn.sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            for msg in conn.reader.feed(chunk):
                conn.record(msg)
                if msg.get("method") == "initialize" and self.answer_initialize:
                    with contextlib.suppress(OSError):
                        conn.send(
                            {
                                "type": "response",
                                "requestId": msg["requestId"],
                                "resultType": "success",
                                "method": "initialize",
                                "handledByClientId": "client-1",
                                "result": {"clientId": "client-1"},
                            }
                        )
