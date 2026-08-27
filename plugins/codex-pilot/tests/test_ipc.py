"""IpcClient dispatch, exercised against an in-process socketpair.

A fake router runs on the far end so these tests cover correlation, discovery
handling, error surfacing and timeouts without touching the real app.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

from codex_pilot.framing import FrameReader, encode_frame
from codex_pilot.ipc import IpcClient, IpcError, IpcTimeout, RouterError


class FakeRouter:
    """Minimal stand-in for Codex Desktop's IpcRouter."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.reader = FrameReader()
        self.received: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self.responder = self._default_responder
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.sock.close()

    def send(self, message: dict[str, Any]) -> None:
        self.sock.sendall(encode_frame(message))

    def _default_responder(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        if msg.get("method") == "initialize":
            return {
                "type": "response",
                "requestId": msg["requestId"],
                "resultType": "success",
                "method": "initialize",
                "handledByClientId": "client-1",
                "result": {"clientId": "client-1"},
            }
        return {
            "type": "response",
            "requestId": msg["requestId"],
            "resultType": "success",
            "method": msg.get("method"),
            "handledByClientId": "owner-9",
            "result": {"echo": msg.get("params")},
        }

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            for msg in self.reader.feed(chunk):
                self.received.append(msg)
                reply = self.responder(msg)
                if reply is not None:
                    try:
                        self.sock.sendall(encode_frame(reply))
                    except OSError:
                        return


@pytest.fixture
def wired():
    ours, theirs = socket.socketpair()
    router = FakeRouter(theirs)
    router.start()
    client = IpcClient(sock=ours, timeout=2.0)
    yield client, router
    client.close()
    router.stop()


def test_initialize_returns_client_id(wired):
    client, _ = wired
    assert client.initialize() == "client-1"
    assert client.client_id == "client-1"


def test_initialize_sends_client_type(wired):
    client, router = wired
    client.initialize(client_type="codex-pilot")
    assert router.received[0]["params"] == {"clientType": "codex-pilot"}


def test_request_carries_the_pinned_version(wired):
    client, router = wired
    client.initialize()
    client.request("thread-owner-discovery", {"hostId": "local", "conversationId": "c1"})
    discovery = router.received[-1]
    assert discovery["version"] == 1
    assert discovery["type"] == "request"


def test_request_returns_full_response_envelope(wired):
    client, _ = wired
    client.initialize()
    resp = client.request("thread-owner-discovery", {"conversationId": "c1"})
    assert resp["handledByClientId"] == "owner-9"


def test_router_error_is_raised_with_its_code(wired):
    client, router = wired
    client.initialize()

    def erroring(msg):
        if msg.get("method") == "initialize":
            return router._default_responder(msg)
        return {
            "type": "response",
            "requestId": msg["requestId"],
            "resultType": "error",
            "error": "no-client-found",
        }

    router.responder = erroring
    with pytest.raises(RouterError) as exc:
        client.request("thread-owner-discovery", {"conversationId": "c1"})
    assert exc.value.error == "no-client-found"


def test_out_of_order_responses_are_correlated_by_request_id(wired):
    client, router = wired
    client.initialize()
    pending: list[dict[str, Any]] = []

    def deferring(msg):
        if msg.get("method") == "initialize":
            return router._default_responder(msg)
        pending.append(msg)
        if len(pending) == 2:
            # answer the second request first
            for m in reversed(pending):
                router.send(
                    {
                        "type": "response",
                        "requestId": m["requestId"],
                        "resultType": "success",
                        "result": {"conv": m["params"]["conversationId"]},
                    }
                )
        return None

    router.responder = deferring
    results: dict[str, Any] = {}
    threads = [
        threading.Thread(
            target=lambda c=c: results.update(
                {c: client.request("thread-owner-discovery", {"conversationId": c})}
            )
        )
        for c in ("first", "second")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert results["first"]["result"]["conv"] == "first"
    assert results["second"]["result"]["conv"] == "second"


def test_timeout_when_router_never_answers(wired):
    client, router = wired
    client.initialize()
    router.responder = lambda msg: None
    with pytest.raises(IpcTimeout):
        client.request("thread-owner-discovery", {"conversationId": "c1"}, timeout=0.4)


def test_client_answers_discovery_requests_with_cannot_handle(wired):
    """We are a client too; the router will ask if we can serve others' requests."""
    client, router = wired
    client.initialize()
    router.send(
        {
            "type": "client-discovery-request",
            "requestId": "disc-1",
            "request": {"method": "thread-follower-start-turn", "params": {}},
        }
    )
    reply = client.wait_for_sent(lambda m: m.get("type") == "client-discovery-response", timeout=2)
    assert reply["requestId"] == "disc-1"
    assert reply["response"] == {"canHandle": False}


def test_broadcasts_are_queued_not_treated_as_responses(wired):
    client, router = wired
    client.initialize()
    router.send(
        {
            "type": "broadcast",
            "method": "thread-stream-following-changed",
            "sourceClientId": "w1",
            "params": {"conversationId": "c1", "hostId": "local", "following": True},
        }
    )
    got = client.next_broadcast(timeout=2)
    assert got["method"] == "thread-stream-following-changed"


def test_request_before_initialize_is_refused(wired):
    client, _ = wired
    with pytest.raises(IpcError):
        client.request("thread-owner-discovery", {"conversationId": "c1"})


# -- connection health: the wedge that started this ---------------------------
#
# A frozen app keeps the socket open and answers nothing, so `is_closed` stays
# false and every request burns its full timeout forever. These pin the strike
# rule that ends that: a timeout only counts when *no* frame arrived while we
# waited, and two in a row retire the connection.


def test_silent_timeouts_retire_the_connection(wired):
    client, router = wired
    client.initialize()
    router.responder = lambda msg: None

    for _ in range(2):
        with pytest.raises(IpcTimeout):
            client.request("thread-owner-discovery", {"conversationId": "c1"}, timeout=0.3)

    assert client.is_closed


def test_one_silent_timeout_is_not_enough(wired):
    """A single stall under load must not tear down a connection that recovers."""
    client, router = wired
    client.initialize()
    router.responder = lambda msg: None

    with pytest.raises(IpcTimeout):
        client.request("thread-owner-discovery", {"conversationId": "c1"}, timeout=0.3)

    assert not client.is_closed


def test_a_frame_arriving_mid_wait_suppresses_the_strike(wired):
    """Traffic proves the socket is alive even when this request goes unanswered."""
    client, router = wired
    client.initialize()

    def silent_but_chatty(msg):
        router.send(
            {
                "type": "broadcast",
                "method": "thread-stream-state-changed",
                "params": {"conversationId": "c1"},
            }
        )
        return None

    router.responder = silent_but_chatty
    for _ in range(3):
        with pytest.raises(IpcTimeout):
            client.request("thread-owner-discovery", {"conversationId": "c1"}, timeout=0.3)

    assert not client.is_closed


def test_a_router_error_response_resets_strikes(wired):
    """`no-client-found` for an unmounted thread is an answer, not silence."""
    client, router = wired
    client.initialize()
    silent = True

    def sometimes(msg):
        if silent:
            return None
        return {
            "type": "response",
            "requestId": msg["requestId"],
            "resultType": "error",
            "error": "no-client-found",
        }

    router.responder = sometimes
    with pytest.raises(IpcTimeout):
        client.request("thread-owner-discovery", {"conversationId": "c1"}, timeout=0.3)

    silent = False
    with pytest.raises(RouterError):
        client.request("thread-owner-discovery", {"conversationId": "c1"}, timeout=1.0)

    silent = True
    with pytest.raises(IpcTimeout):
        client.request("thread-owner-discovery", {"conversationId": "c1"}, timeout=0.3)

    # The error response cleared the first strike, so this is strike one again.
    assert not client.is_closed


def test_retiring_the_connection_never_resends(wired):
    """A dead connection is replaced, not retried: the outcome stays unknown."""
    client, router = wired
    client.initialize()
    router.responder = lambda msg: None

    for _ in range(2):
        with pytest.raises(IpcTimeout):
            client.request("thread-follower-start-turn", {"conversationId": "c1"}, timeout=0.3)

    turns = [m for m in router.received if m.get("method") == "thread-follower-start-turn"]
    assert len(turns) == 2
    assert len({m["requestId"] for m in turns}) == 2


def test_connection_reset_broadcast_closes_the_client(wired):
    """Decoded, not yet verified live: the router's own reset signal."""
    client, router = wired
    client.initialize()
    router.send({"type": "broadcast", "method": "ipc-connection-reset", "params": {}})
    deadline = time.monotonic() + 2.0
    while not client.is_closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.is_closed
