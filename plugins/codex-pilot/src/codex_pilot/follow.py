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


class SeqCounter:
    """Monotonic event sequence shared across every instance's manager."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    def peek(self) -> int:
        """The highest sequence issued so far, for spotting an impossible cursor."""
        with self._lock:
            return self._value


EVENT_TURN_STARTED = "turn_started"
EVENT_TURN_COMPLETED = "turn_completed"
EVENT_REQUEST_PENDING = "request_pending"
EVENT_REQUEST_RESOLVED = "request_resolved"
EVENT_RESYNC = "resync"
EVENT_FOLLOW_LOST = "follow_lost"
# A detached run has no stream to follow, so its completion is reported by
# watching the process instead. Same event name as a streamed turn ending,
# so one wait loop covers both routes.
EVENT_RUN_FAILED = "run_failed"


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
    awaiting_snapshot: bool = False
    # When the outstanding snapshot request went out, so a request the app
    # was not ready to answer can be repeated instead of waited on forever.
    awaiting_since: float | None = None
    lost_reason: str | None = None
    collected_dropped: int = 0
    # True for a thread we only report *about* (a detached run) rather than
    # subscribe to. It buffers events but is not a live follow, so it must
    # not be resynced, marked lost, or counted as followed.
    external: bool = False

    @property
    def projected(self) -> ThreadState | None:
        if self.state is None:
            return None
        return projection.project(
            {
                "params": {
                    "change": {
                        "type": "snapshot",
                        "revision": self.revision,
                        "conversationState": self.state,
                    }
                }
            }
        )


class FollowManager:
    """Tracks followed threads for one instance and derives events from them."""

    def __init__(self, instance: str, seq: SeqCounter | None = None) -> None:
        self.instance = instance
        self._shared_seq = seq
        self._threads: dict[str, FollowedThread] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._seq = 0
        self._resync_requests: list[str] = []

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
            return sorted(t for t, v in self._threads.items() if not v.external)

    def is_following(self, thread_id: str) -> bool:
        with self._lock:
            tracked = self._threads.get(thread_id)
            return tracked is not None and not tracked.external

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
                    tracked.awaiting_snapshot = False
                    tracked.awaiting_since = None
                    tracked.lost_reason = None
            elif kind == "patches":
                # A missing baseRevision is treated as a gap: without it there is
                # no way to know the patch belongs to the state we hold.
                mismatched = base is None or base != tracked.revision
                if tracked.state is None or mismatched:
                    produced.extend(
                        self._begin_resync(
                            tracked,
                            {"reason": "revision gap", "expected": base, "held": tracked.revision},
                        )
                    )
                    self._wake.notify_all()
                    return produced
                try:
                    tracked.state = projection.apply_patches(tracked.state, payload or [])
                    tracked.revision = revision
                except PatchError as exc:
                    produced.extend(self._begin_resync(tracked, {"reason": str(exc)}))
                    self._wake.notify_all()
                    return produced
            else:
                return []

            produced.extend(self._diff(tracked))
            if produced:
                self._wake.notify_all()
            return produced

    def _begin_resync(self, tracked: FollowedThread, data: dict[str, Any]) -> list[Event]:
        """Drop the state and ask for a fresh snapshot, exactly once per gap.

        `held` is captured before clearing, or the diagnostic always reads None —
        which is precisely the value you do not want when debugging a gap. The
        `awaiting_snapshot` flag stops a broken stream turning into resync spam
        that evicts every real event from the buffer.
        """
        data = data if "held" in data else {**data, "held": tracked.revision}
        already = tracked.awaiting_snapshot
        tracked.state = None
        tracked.revision = None
        tracked.awaiting_snapshot = True
        tracked.awaiting_since = time.monotonic()
        if already:
            return []
        self._resync_requests.append(tracked.thread_id)
        return [self._emit(tracked, EVENT_RESYNC, data)]

    def health_of(self, thread_id: str) -> dict[str, Any]:
        """What this follow can and cannot currently tell you.

        `pending` is only meaningful alongside `pending_known`. An empty list on
        a thread whose state we cannot read means "no idea", not "nothing
        pending" -- conflating the two is how a blocked thread goes unnoticed,
        which is the whole reason this is reported rather than inferred from an
        absence in `following`.
        """
        with self._lock:
            return self._health_locked(thread_id)

    def _health_locked(self, thread_id: str) -> dict[str, Any]:
        tracked = self._threads.get(thread_id)
        if tracked is None or tracked.external:
            return {
                "health": "not_following",
                "reason": None,
                "pending": [],
                "pending_known": False,
            }
        state = tracked.projected
        if tracked.lost_reason is not None:
            health, reason = "lost", tracked.lost_reason
        elif tracked.awaiting_snapshot or state is None:
            health, reason = "resyncing", "awaiting a fresh snapshot"
        else:
            health, reason = "ok", None
        known = state is not None
        return {
            "health": health,
            "reason": reason,
            "pending": [p.as_dict() for p in state.pending] if known and state else [],
            "pending_known": known,
        }

    def resync_all(self, reason: str) -> list[Event]:
        """Re-request a snapshot for every followed thread after a reconnect.

        A replaced connection means the app holds no record of our
        subscriptions, so every follow is silently dead until it is asked for
        again. This deliberately bypasses the `awaiting_snapshot` guard that
        `_begin_resync` honours: a thread already awaiting a snapshot when the
        socket died is exactly the one that would otherwise never be re-queued,
        and would sit silent forever.
        """
        with self._lock:
            produced: list[Event] = []
            for tracked in self._threads.values():
                tracked.state = None
                tracked.revision = None
                tracked.awaiting_snapshot = True
                # Stamped so `stale_awaiting` can pick this up again: an app
                # that is still starting, or holding no threads, never answers
                # the first re-subscribe.
                tracked.awaiting_since = time.monotonic()
                if tracked.thread_id not in self._resync_requests:
                    self._resync_requests.append(tracked.thread_id)
                produced.append(self._emit(tracked, EVENT_RESYNC, {"reason": reason}))
            if produced:
                self._wake.notify_all()
            return produced

    def requeue_resync(self, thread_ids: list[str]) -> None:
        """Put back ids whose re-subscribe broadcast failed after being taken.

        `take_resync_requests` pops, so a failed send would otherwise lose them
        outright -- and `_begin_resync` will not queue them again while
        `awaiting_snapshot` holds.
        """
        with self._lock:
            for thread_id in thread_ids:
                if thread_id in self._threads and thread_id not in self._resync_requests:
                    self._resync_requests.append(thread_id)

    def stale_awaiting(self, older_than: float) -> list[str]:
        """Follows still waiting on a snapshot they asked for a while ago.

        A resync is requested once and then latched, which is right for a gap in
        a stream the app is actively sending: one request is enough. It is wrong
        after a reconnect, where the request can reach an app that is still
        starting up, or that has not reopened the thread at all -- verified
        live, where the re-subscribe went out to an app holding no threads and
        the follow then sat silent with nothing left to ask again.

        Re-queues them for the pump. Deliberately does not emit another event:
        the latch still does its original job of keeping resync spam from
        evicting real events from the buffer.
        """
        now = time.monotonic()
        with self._lock:
            due = [
                t.thread_id
                for t in self._threads.values()
                if not t.external
                and t.awaiting_snapshot
                and t.awaiting_since is not None
                and now - t.awaiting_since >= older_than
            ]
            for thread_id in due:
                self._threads[thread_id].awaiting_since = now
            pending = set(self._resync_requests)
            self._resync_requests.extend(t for t in due if t not in pending)
            return due

    def take_resync_requests(self) -> list[str]:
        """Threads that need a fresh snapshot re-requested from the app.

        A gap is only recoverable if somebody re-subscribes; without this the
        follow stays wedged forever, silently reporting nothing.
        """
        with self._lock:
            pending, self._resync_requests = self._resync_requests, []
            return pending

    def lost(self, thread_id: str, reason: str) -> None:
        with self._lock:
            tracked = self._threads.get(thread_id)
            if tracked is None or tracked.lost_reason == reason:
                # Emit once per transition; a retry loop would otherwise evict
                # the whole buffer with duplicates.
                return
            tracked.state = None
            tracked.revision = None
            tracked.lost_reason = reason
            self._emit(tracked, EVENT_FOLLOW_LOST, {"reason": reason})
            self._wake.notify_all()

    def emit_external(self, thread_id: str, kind: str, data: dict[str, Any]) -> None:
        """Record an event for a thread that has no stream to follow.

        A detached run is invisible to the app's broadcasts -- it is not mounted
        there -- so its completion would otherwise never reach `collect_events`,
        and an orchestrator waiting on a fan-out would wait forever for threads
        it started itself. Buffering it here is what lets one wait loop cover
        both routes.
        """
        with self._lock:
            tracked = self._threads.get(thread_id)
            if tracked is None:
                tracked = FollowedThread(instance=self.instance, thread_id=thread_id, external=True)
                self._threads[thread_id] = tracked
            self._emit(tracked, kind, data)
            self._wake.notify_all()

    # -- event derivation ---------------------------------------------------

    def _emit(self, tracked: FollowedThread, kind: str, data: dict[str, Any]) -> Event:
        # A shared counter across instances keeps one cursor meaningful; with
        # per-manager sequences the busier instance masks the quieter one.
        if self._shared_seq is not None:
            seq = self._shared_seq.next()
        else:
            self._seq += 1
            seq = self._seq
        event = Event(
            seq=seq, instance=tracked.instance, thread=tracked.thread_id, type=kind, data=data
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
                    following = sorted(t for t, v in self._threads.items() if not v.external)
                    # Health covers what the caller asked about as well as what
                    # we hold: a thread missing from `following` after a restart
                    # is exactly the case that needs saying out loud.
                    asked = set(threads or []) | set(following)
                    return {
                        "events": [e.as_dict() for e in events],
                        "cursor": events[-1].seq if events else after,
                        "dropped": dropped,
                        "following": following,
                        "threads": {t: self._health_locked(t) for t in sorted(asked)},
                    }
                self._wake.wait(timeout=min(1.0, max(0.05, deadline - time.monotonic())))

    def _gather(self, threads: list[str] | None, after: int) -> tuple[list[Event], int]:
        wanted = set(threads) if threads else None
        events: list[Event] = []
        dropped = 0
        for thread_id, tracked in self._threads.items():
            if wanted is not None and thread_id not in wanted:
                continue
            # Report the drops since the caller last looked, not a lifetime
            # total that would re-alarm on every poll forever.
            dropped += max(0, tracked.dropped - tracked.collected_dropped)
            tracked.collected_dropped = tracked.dropped
            events.extend(e for e in tracked.events if e.seq > after)
        events.sort(key=lambda e: e.seq)
        return events, dropped
