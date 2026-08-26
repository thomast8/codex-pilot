"""Client for Codex Desktop's local IPC router.

The Desktop app's Electron main process listens on a unix stream socket at
`$CODEX_HOME/ipc/ipc.sock` and acts as a router between connected clients
(renderer windows, helper processes). A client connects, sends `initialize` to
get a clientId, and then sends requests that the router forwards to whichever
other client claims it can handle them.

Routing detail worth knowing: without `targetClientId` the router asks *every*
other client (`client-discovery-request`) and takes the first that answers
`canHandle: true`. With `targetClientId` it asks only that one. Either way a
client that cannot handle the method -- including one whose version does not
match -- declines, and the caller sees `no-client-found`.

We are a client on this bus too, so the router will ask us to handle other
clients' requests. We always decline.
"""

from __future__ import annotations

import contextlib
import os
import queue
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .framing import FrameError, FrameReader, encode_frame
from .registry import build_broadcast, build_request

DEFAULT_TIMEOUT = 15.0  # the router's own discovery timeout is 10s; outlast it
BROADCAST_QUEUE_MAX = 2000


class IpcError(Exception):
    """Transport or protocol failure talking to the router."""


class IpcTimeout(IpcError):
    """No response arrived in time. The request may still have been delivered."""


class IpcUnavailable(IpcError):
    """The socket is absent or refused -- Codex Desktop is probably not running."""


class RouterError(IpcError):
    """The router or the target client returned resultType: error."""

    def __init__(self, error: str, method: str | None = None) -> None:
        self.error = error
        self.method = method
        super().__init__(f"{method or 'request'} failed: {error}")


def default_socket_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home) if codex_home else Path.home() / ".codex"
    return base / "ipc" / "ipc.sock"


class IpcClient:
    """Connection to the Codex Desktop IPC router.

    Long-lived by design: one connection, one handshake, reused across calls, so
    a follow subscription can stay open between requests.
    """

    def __init__(
        self,
        sock: socket.socket | None = None,
        socket_path: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._path = socket_path or default_socket_path()
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._broadcasts: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=BROADCAST_QUEUE_MAX)
        self._sent: list[dict[str, Any]] = []
        self._sent_event = threading.Condition()
        self._reader = FrameReader()
        self._closed = threading.Event()
        self._fatal: BaseException | None = None
        self.client_id: str | None = None

        self._sock = sock if sock is not None else self._connect()
        self._pump = threading.Thread(target=self._read_loop, daemon=True)
        self._pump.start()

    # -- connection ---------------------------------------------------------

    def _connect(self) -> socket.socket:
        if not self._path.exists():
            raise IpcUnavailable(
                f"no IPC socket at {self._path} -- Codex Desktop is not running. "
                "For threads the app does not own, fall back to `codex-drive send`."
            )
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(self._path))
        except OSError as exc:
            s.close()
            raise IpcUnavailable(f"cannot connect to {self._path}: {exc}") from exc
        return s

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    def close(self) -> None:
        self._closed.set()
        with contextlib.suppress(OSError):
            self._sock.close()

    # -- read pump ----------------------------------------------------------

    def _read_loop(self) -> None:
        while not self._closed.is_set():
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            try:
                for msg in self._reader.feed(chunk):
                    self._dispatch(msg)
            except FrameError as exc:
                self._fatal = exc
                break
        self._closed.set()
        # Unblock anyone waiting on a response.
        with self._lock:
            waiters = list(self._pending.values())
        for w in waiters:
            w.put({"__disconnected__": True})

    def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "response":
            rid = msg.get("requestId")
            with self._lock:
                waiter = self._pending.get(str(rid))
            if waiter is not None:
                waiter.put(msg)
            return
        if kind == "client-discovery-request":
            # The router is asking whether we can serve someone else's request.
            self._send(
                {
                    "type": "client-discovery-response",
                    "requestId": msg.get("requestId"),
                    "response": {"canHandle": False},
                }
            )
            return
        if kind == "broadcast":
            try:
                self._broadcasts.put_nowait(msg)
            except queue.Full:
                # Following a busy thread can flood; drop oldest to stay bounded.
                try:
                    self._broadcasts.get_nowait()
                    self._broadcasts.put_nowait(msg)
                except queue.Empty:
                    pass

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._sock.sendall(encode_frame(message))
        except OSError as exc:
            raise IpcError(f"send failed: {exc}") from exc
        with self._sent_event:
            self._sent.append(message)
            self._sent_event.notify_all()

    # -- requests -----------------------------------------------------------

    def initialize(self, client_type: str = "codex-pilot") -> str:
        env = build_request("initialize", {"clientType": client_type})
        resp = self._exchange(env, self._timeout)
        client_id = (resp.get("result") or {}).get("clientId")
        if not isinstance(client_id, str):
            raise IpcError(f"initialize returned no clientId: {resp}")
        self.client_id = client_id
        return client_id

    def request(
        self,
        method: str,
        params: dict[str, Any],
        target_client_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self.client_id is None:
            raise IpcError("call initialize() before sending requests")
        env = build_request(method, params, target_client_id=target_client_id)
        resp = self._exchange(env, timeout or self._timeout)
        if resp.get("resultType") == "error":
            raise RouterError(str(resp.get("error")), method)
        return resp

    def broadcast(self, method: str, params: dict[str, Any]) -> None:
        if self.client_id is None:
            raise IpcError("call initialize() before broadcasting")
        self._send(build_broadcast(method, params))

    def _exchange(self, envelope: dict[str, Any], timeout: float) -> dict[str, Any]:
        rid = str(envelope["requestId"])
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[rid] = waiter
        try:
            self._send(envelope)
            try:
                resp = waiter.get(timeout=timeout)
            except queue.Empty:
                raise IpcTimeout(
                    f"{envelope['method']} timed out after {timeout}s -- outcome unknown, "
                    "check the Codex Desktop UI before retrying"
                ) from None
            if resp.get("__disconnected__"):
                raise IpcError(f"connection closed while awaiting {envelope['method']}")
            return resp
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    # -- broadcasts ---------------------------------------------------------

    def next_broadcast(self, timeout: float = 3.0) -> dict[str, Any] | None:
        try:
            return self._broadcasts.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_broadcasts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._broadcasts.get_nowait())
            except queue.Empty:
                return out

    # -- test support -------------------------------------------------------

    def wait_for_sent(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 2.0
    ) -> dict[str, Any]:
        """Wait until we have sent a message matching `predicate`, and return it."""
        with self._sent_event:
            found = self._sent_event.wait_for(
                lambda: next((m for m in self._sent if predicate(m)), None) is not None,
                timeout=timeout,
            )
            if not found:
                raise IpcTimeout("no matching message was sent")
            return next(m for m in self._sent if predicate(m))
