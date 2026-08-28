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
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .framing import FrameError, FrameReader, encode_frame
from .registry import build_broadcast, build_request

DEFAULT_TIMEOUT = 15.0  # the router's own discovery timeout is 10s; outlast it
BROADCAST_QUEUE_MAX = 2000
# Consecutive timeouts during which *no* frame arrived at all before we give up
# on the connection. A frozen app (a modal dialog, a hung main process) keeps
# the socket open and answers nothing, so `is_closed` never trips and every
# request burns its full timeout forever. One strike is not enough: a single
# stall under system-wide load should not retire a connection that recovers.
STALE_STRIKE_LIMIT = 2
# Sent by the router when it resets the bus. Decoded from the bundle and pinned
# in the version registry, but never observed live -- treated as fatal on the
# theory that acting on it early is free and ignoring it is not.
CONNECTION_RESET = "ipc-connection-reset"


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


def _stat_identity(path: Path) -> tuple[int, int] | None:
    """(st_dev, st_ino) of a socket, or None when it is not there right now."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


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
        # sendall is not atomic across threads, and there are several
        # senders: the follow pump broadcasts while tool calls send
        # requests. Interleaved frames would desync the length-prefixed
        # stream, which the reader answers by destroying the socket.
        self._send_lock = threading.Lock()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._broadcasts: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=BROADCAST_QUEUE_MAX)
        self._sent: list[dict[str, Any]] = []
        self._sent_event = threading.Condition()
        self._reader = FrameReader()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.dropped_broadcasts = 0
        self._closed = threading.Event()
        self._fatal: BaseException | None = None
        self.client_id: str | None = None
        # (st_dev, st_ino) of the socket we connected through. The path is stable
        # across an app restart but the inode is not, so this is what tells a
        # re-bound socket apart from the one we still hold.
        self.socket_identity: tuple[int, int] | None = None
        # Monotonic stamp of the last frame of any kind. 0.0 until one arrives,
        # which matters because `initialize` is itself the first exchange.
        self._last_frame = 0.0
        self._strikes = 0
        # Start of the most recent counted stall, so several requests waiting
        # on one silence score it once between them.
        self._last_strike_at = 0.0

        self._sock = sock if sock is not None else self._connect()
        self._pump = threading.Thread(target=self._read_loop, daemon=True)
        self._pump.start()

    # -- connection ---------------------------------------------------------

    def matches_socket(self) -> bool:
        """Whether the socket at our path is still the one we connected through.

        The path is stable across an app restart; the inode is not, because the
        app unlinks and re-binds. A client built without connecting (a test
        double over a socketpair) has no identity and is never called stale.
        """
        if self.socket_identity is None:
            return True
        return _stat_identity(self._path) == self.socket_identity

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
        # Stamped here rather than by the caller: a stat afterwards can catch a
        # re-bind that happened during connect and record the new inode against
        # the old connection, which would mask the staleness forever.
        self.socket_identity = _stat_identity(self._path)
        return s

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    def close(self) -> None:
        self._closed.set()
        # Shut the socket down before closing it: closing an fd another thread
        # is blocked in recv() on does not reliably wake it on macOS.
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._sock.close()

    # -- read pump ----------------------------------------------------------

    def add_broadcast_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Register a consumer that sees every broadcast.

        Listeners exist because a single shared queue makes consumers compete:
        whoever polls first takes the frame and everyone else never sees it. A
        persistent follow and a one-off status read need the same frames.
        """
        with self._lock:
            self._listeners.append(listener)

    def _read_loop(self) -> None:
        try:
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
                except Exception as exc:  # noqa: BLE001 - the pump must not die silently
                    self._fatal = exc
                    break
        finally:
            # Whatever happened, the connection is finished: mark it closed and
            # release every waiter, or callers keep using a dead client and each
            # request burns its full timeout.
            self._closed.set()
            with self._lock:
                waiters = list(self._pending.values())
            for w in waiters:
                w.put({"__disconnected__": True})

    def _dispatch(self, msg: dict[str, Any]) -> None:
        # Any frame at all proves the far end is alive, so this is what the
        # stale-connection check reads. Recorded before dispatch, not after,
        # so a listener that raises cannot cost us the liveness evidence.
        self._last_frame = time.monotonic()
        kind = msg.get("type")
        if msg.get("method") == CONNECTION_RESET:
            self._fatal = IpcError("router sent ipc-connection-reset")
            self.close()
            return
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
            with self._lock:
                listeners = list(self._listeners)
            for listener in listeners:
                with contextlib.suppress(Exception):  # one bad listener must not kill the pump
                    listener(msg)
            try:
                self._broadcasts.put_nowait(msg)
            except queue.Full:
                # Following a busy thread can flood; drop oldest to stay bounded.
                try:
                    self._broadcasts.get_nowait()
                    self._broadcasts.put_nowait(msg)
                    self.dropped_broadcasts += 1
                except queue.Empty:
                    pass

    def _send(self, message: dict[str, Any]) -> None:
        frame = encode_frame(message)
        try:
            with self._send_lock:
                self._sock.sendall(frame)
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
        silence_counts: bool = True,
    ) -> dict[str, Any]:
        """Send a request and wait for its answer.

        `silence_counts=False` is for a caller whose timeout is deliberately
        shorter than the answer it is asking for -- a bounded mountedness probe,
        where no reply inside a second is the useful signal rather than a fault.
        Such silence is arranged, so it says nothing about the connection and
        must not count toward retiring it. Everything else is unchanged, and the
        default keeps the ordinary meaning: silence on a request that should
        have been answered is evidence the socket has stopped talking.
        """
        if self.client_id is None:
            raise IpcError("call initialize() before sending requests")
        env = build_request(method, params, target_client_id=target_client_id)
        resp = self._exchange(env, timeout or self._timeout, silence_counts=silence_counts)
        if resp.get("resultType") == "error":
            raise RouterError(str(resp.get("error")), method)
        return resp

    def broadcast(self, method: str, params: dict[str, Any]) -> None:
        if self.client_id is None:
            raise IpcError("call initialize() before broadcasting")
        self._send(build_broadcast(method, params))

    def _record_silent_timeout(self, sent_at: float, counts: bool = True) -> bool:
        """Count a timeout against the connection, and retire it at the limit.

        Only a timeout with *no* frame arriving for its whole duration is
        evidence about the connection rather than about one request: a thread
        the app holds but does not render legitimately costs the router's full
        ~10s discovery timeout, but that arrives as a `no-client-found`
        response, and any broadcast in the meantime proves the socket is live.

        `counts=False` is a caller whose deadline was deliberately shorter than
        the answer -- its silence is arranged and says nothing. Note what that
        does *not* excuse: a frame that arrived during the wait is evidence
        about the connection no matter who set the deadline, so the reset below
        still runs. Skipping it would let a probe that proved the socket alive
        leave a stale strike standing, and retire a live connection one genuine
        timeout early.

        Returns whether this call retired the connection. Retiring only closes
        it -- nothing is re-sent, so a request whose outcome is unknown stays
        unknown, and the replacement connection is built lazily by the caller.
        """
        with self._lock:
            if self._last_frame >= sent_at:
                self._strikes = 0
                return False
            if not counts:
                return False
            # Count stall *windows*, not requests. Several calls can be waiting
            # on one silent connection, and scoring each of them separately made
            # the limit effectively one -- a single stall under load would retire
            # a connection that was about to recover.
            if sent_at <= self._last_strike_at:
                return False
            self._last_strike_at = time.monotonic()
            self._strikes += 1
            if self._strikes < STALE_STRIKE_LIMIT:
                return False
            self._fatal = IpcError(
                f"no frames received across {self._strikes} consecutive timeouts"
            )
        self.close()
        return True

    def _exchange(
        self, envelope: dict[str, Any], timeout: float, silence_counts: bool = True
    ) -> dict[str, Any]:
        rid = str(envelope["requestId"])
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[rid] = waiter
        try:
            sent_at = time.monotonic()
            self._send(envelope)
            try:
                resp = waiter.get(timeout=timeout)
            except queue.Empty:
                retired = self._record_silent_timeout(sent_at, counts=silence_counts)
                detail = (
                    " -- no frames arrived on this connection either, so it has been "
                    "retired; the next call re-handshakes"
                    if retired
                    else ""
                )
                raise IpcTimeout(
                    f"{envelope['method']} timed out after {timeout}s -- outcome unknown, "
                    f"check the Codex Desktop UI before retrying{detail}"
                ) from None
            if resp.get("__disconnected__"):
                raise IpcError(f"connection closed while awaiting {envelope['method']}")
            # An answer of any kind, including a router error, proves the far end
            # is talking to us. Only silence counts against the connection.
            with self._lock:
                self._strikes = 0
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
