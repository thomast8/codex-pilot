"""Persistent follows: keep a thread's state current and emit what changed.

A follow is a subscription to one thread's `thread-stream-state-changed`
broadcasts. The app sends a full snapshot first, then incremental patches whose
`baseRevision` must match the revision we hold. We keep the projected state and
turn each transition into a semantic event -- `turn_completed` is the one that
matters for orchestration, because it is how you learn a thread went idle
without watching it.

Two deliberate choices:

**A gap is never papered over.** If a patch arrives whose `baseRevision` does
not match what we hold, the state is dropped and re-seeded from a fresh
snapshot, and a `resync` event is emitted. Applying a patch to the wrong
baseline yields state that looks current and is not, which is worse than
admitting the gap.

**Delivery is poll-with-long-poll.** MCP has no server-initiated push into a
Claude Code session, so events accumulate in a bounded per-thread buffer and
`collect_events` drains them, optionally waiting. A dropped-event count is
surfaced rather than silently losing history.

A follow only works while Codex Desktop has the thread *mounted* -- a thread the
app holds open but is not rendering sends nothing at all. That is an app
constraint, not something this can work around.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import snapshot as projection
from .snapshot import PatchError, ThreadState

BUFFER_SIZE = 200

EVENT_TURN_STARTED = "turn_started"
EVENT_TURN_COMPLETED = "turn_completed"
EVENT_REQUEST_PENDING = "request_pending"
EVENT_REQUEST_RESOLVED = "request_resolved"
EVENT_RESYNC = "resync"
EVENT_FOLLOW_LOST = "follow_lost"


@dataclass(frozen=True)
class Event:
    seq: int
    instance: str
    thread: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "instance": self.instance,
            "thread": self.thread,
            "type": self.type,
            "data": self.data,
        }


@dataclass
class FollowedThread:
    instance: str
    thread_id: str
    state: dict[str, Any] | None = None
    revision: int | None = None
    events: deque[Event] = field(default_factory=lambda: deque(maxlen=BUFFER_SIZE))
    dropped: int = 0
    last_runtime: str | None = None
    last_requests: frozenset[Any] = frozenset()

    @property
    def projected(self) -> ThreadState | None:
        if self.state is None:
            return None
        return projection.project(
            {"params": {"change": {"type": "snapshot", "conversationState": self.state}}}
        )


class FollowManager:
    """Tracks followed threads for one instance and derives events from them."""

    def __init__(self, instance: str) -> None:
        self.instance = instance
        self._threads: dict[str, FollowedThread] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._seq = 0

    # -- registration -------------------------------------------------------

    def follow(self, thread_id: str) -> None:
        with self._lock:
            self._threads.setdefault(
                thread_id, FollowedThread(instance=self.instance, thread_id=thread_id)
            )

    def unfollow(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)

    @property
    def followed(self) -> list[str]:
        with self._lock:
            return sorted(self._threads)

    def is_following(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._threads

    def state_of(self, thread_id: str) -> ThreadState | None:
        with self._lock:
            tracked = self._threads.get(thread_id)
        return tracked.projected if tracked else None

    # -- ingest -------------------------------------------------------------

    def handle_frame(self, frame: dict[str, Any]) -> list[Event]:
        """Feed one `thread-stream-state-changed` broadcast in.

        Returns the events it produced, and buffers them for `collect`.
        """
        params = frame.get("params") or {}
        thread_id = params.get("conversationId")
        if not isinstance(thread_id, str):
            return []
        with self._lock:
            tracked = self._threads.get(thread_id)
            if tracked is None:
                return []
            kind, base, revision, payload = projection.state_from_frame(frame)
            produced: list[Event] = []

            if kind == "snapshot":
                if isinstance(payload, dict):
                    tracked.state = payload
                    tracked.revision = revision
            elif kind == "patches":
                if tracked.state is None or (base is not None and base != tracked.revision):
                    # A patch against a baseline we do not hold cannot be
                    # applied; drop what we have and wait for a fresh snapshot.
                    tracked.state = None
                    tracked.revision = None
                    produced.append(
                        self._emit(
                            tracked,
                            EVENT_RESYNC,
                            {"reason": "revision gap", "expected": base, "held": tracked.revision},
                        )
                    )
                    self._wake.notify_all()
                    return produced
                try:
                    tracked.state = projection.apply_patches(tracked.state, payload or [])
                    tracked.revision = revision
                except PatchError as exc:
                    tracked.state = None
                    tracked.revision = None
                    produced.append(self._emit(tracked, EVENT_RESYNC, {"reason": str(exc)}))
                    self._wake.notify_all()
                    return produced
            else:
                return []

            produced.extend(self._diff(tracked))
            if produced:
                self._wake.notify_all()
            return produced

    def lost(self, thread_id: str, reason: str) -> None:
        with self._lock:
            tracked = self._threads.get(thread_id)
            if tracked is None:
                return
            tracked.state = None
            tracked.revision = None
            self._emit(tracked, EVENT_FOLLOW_LOST, {"reason": reason})
            self._wake.notify_all()

    # -- event derivation ---------------------------------------------------

    def _emit(self, tracked: FollowedThread, kind: str, data: dict[str, Any]) -> Event:
        self._seq += 1
        event = Event(
            seq=self._seq, instance=tracked.instance, thread=tracked.thread_id, type=kind, data=data
        )
        if len(tracked.events) == tracked.events.maxlen:
            tracked.dropped += 1
        tracked.events.append(event)
        return event

    def _diff(self, tracked: FollowedThread) -> list[Event]:
        state = tracked.projected
        if state is None:
            return []
        out: list[Event] = []

        if state.runtime != tracked.last_runtime:
            if state.runtime == projection.RUNTIME_ACTIVE:
                out.append(self._emit(tracked, EVENT_TURN_STARTED, {"turn_id": state.turn_id}))
            elif state.runtime == projection.RUNTIME_IDLE and tracked.last_runtime is not None:
                # The orchestration signal: this thread is free for more work.
                out.append(self._emit(tracked, EVENT_TURN_COMPLETED, {}))
            tracked.last_runtime = state.runtime

        now_requests = {p.request_id for p in state.pending}
        for pending in state.pending:
            if pending.request_id not in tracked.last_requests:
                out.append(
                    self._emit(
                        tracked,
                        EVENT_REQUEST_PENDING,
                        {
                            "request_id": pending.request_id,
                            "kind": pending.kind,
                            "summary": pending.summary,
                            "reason": pending.reason,
                            "available_decisions": pending.available_decisions,
                        },
                    )
                )
        for gone in tracked.last_requests - now_requests:
            out.append(self._emit(tracked, EVENT_REQUEST_RESOLVED, {"request_id": gone}))
        tracked.last_requests = frozenset(now_requests)
        return out

    # -- delivery -----------------------------------------------------------

    def collect(
        self, threads: list[str] | None = None, after: int = 0, wait_seconds: float = 0.0
    ) -> dict[str, Any]:
        """Drain buffered events, optionally waiting for the first one."""
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            with self._lock:
                events, dropped = self._gather(threads, after)
                if events or time.monotonic() >= deadline:
                    return {
                        "events": [e.as_dict() for e in events],
                        "cursor": events[-1].seq if events else after,
                        "dropped": dropped,
                        "following": sorted(self._threads),
                    }
                self._wake.wait(timeout=min(1.0, max(0.05, deadline - time.monotonic())))

    def _gather(self, threads: list[str] | None, after: int) -> tuple[list[Event], int]:
        wanted = set(threads) if threads else None
        events: list[Event] = []
        dropped = 0
        for thread_id, tracked in self._threads.items():
            if wanted is not None and thread_id not in wanted:
                continue
            dropped += tracked.dropped
            events.extend(e for e in tracked.events if e.seq > after)
        events.sort(key=lambda e: e.seq)
        return events, dropped
