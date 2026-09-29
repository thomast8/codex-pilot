"""Thread actions: resolve, discover the owner, then act on it.

Two invariants hold everywhere in this module.

**Instance binding is atomic.** A thread reference resolves to an
`(instance, thread_id)` pair, and every path used for that call -- socket, lock
dir, rollout, archive -- comes from that same instance. Thread ids are only
unique within a CODEX_HOME, so choosing a socket from one instance and a thread
id from another would drive the wrong app with no error.

**No mutation without a same-call owner discovery.** Every mutating request is
addressed with `targetClientId` taken from a discovery performed in that call.
The app double-checks with `assertThreadFollowerOwner`, but addressing the owner
directly means we never rely on the router's broadcast fallback picking right.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from . import frontmost, payloads
from . import ipc as ipc_module
from .follow import EVENT_RUN_FAILED, EVENT_TURN_COMPLETED, FollowManager, SeqCounter
from .instances import Instance, discover_instances, stock_app
from .ipc import IpcClient, IpcError, IpcUnavailable, RouterError
from .resume import DetachedRun, DetachedRunner, scan_for_thread_id
from .threads import (
    AmbiguousThreadError,
    ThreadError,
    ThreadInfo,
    ThreadStore,
    UnknownThreadError,
    serving_app,
)

OWNER_DISCOVERY = "thread-owner-discovery"
FOLLOWING_CHANGED = "thread-stream-following-changed"
STREAM_STATE_CHANGED = "thread-stream-state-changed"
SNAPSHOT_WAIT_SECONDS = 3.0
# How long a turn may sit in progress without a server-assigned id before we
# call it stuck rather than young. The id normally lands with the turn's first
# stream event, so this is orders of magnitude past the honest case; it exists
# to be certain, not to be prompt.
# How long a turn may sit in progress without a server-assigned id before we
# call it stuck rather than merely young. The app sends `turn/start` with its
# own 30s timeout, so an id that has not arrived within 30s of the turn
# starting is not in flight any more -- the request it would have come back on
# has already given up. Twice that is chosen to be certain, not to be prompt.
STALLED_TURN_SECONDS = 60.0
# The pre-check reads state that is already known; it does not go looking. A
# thread with a live follow answers from memory, a mounted one answered in
# under 0.3s when measured, and an app-held thread no window renders never
# answers at all -- and that last case is the common one, so the wait it costs
# has to stay small. Falling through on silence only means steering as before.
STALLED_TURN_PROBE_SECONDS = 1.0
# How long an unanswered snapshot request waits before being asked again.
# Long enough not to spam an app that is merely slow, short enough that a
# thread the app reopens after a restart starts streaming without a nudge.
RESYNC_RETRY_SECONDS = 15.0
# Ceiling on the pump's backoff after an error it did not expect. Long enough
# that a fault firing every tick costs nothing, short enough that the pump is
# still there to recover once whatever caused it clears.
PUMP_FAULT_MAX_BACKOFF = 30.0
# Finished runs stay listed so threads we started remain visible; the cap
# keeps a long orchestration session from growing the map without end.
MAX_TRACKED_RUNS = 200

# Route values. `detached_running` is a lock held by a writer that is not the
# app -- one of our own `codex exec` children, or anyone else's -- so neither
# route works until it exits. `unknown` is the honest fourth: a lock state we
# could not establish. It is not `detached`, because resuming onto a lock that
# may be held is the one mistake that corrupts a rollout.
ROUTE_DESKTOP = "desktop"
ROUTE_DETACHED = "detached"
ROUTE_RUNNING = "detached_running"
ROUTE_UNKNOWN = "unknown"

# An unmounted thread costs the router's full discovery timeout (~10s
# measured) before it answers no-client-found, while a mounted one
# answers in ~0.4s. Probing serially is therefore unusable on a real
# instance; requests are multiplexed by id, so they go concurrently.
CENSUS_WORKERS = 8

# What `focus_thread` will wait to learn a thread is already mounted before it
# gives up asking and fires the link anyway. A mounted thread answers in ~0.4s,
# so this is headroom rather than a guess; the router's ~10s no-client-found is
# deliberately not waited for, because by then the link would have been the
# cheaper way to find out.
FOCUS_PROBE_TIMEOUT_SECONDS = 1.0

# Ceiling on one `open` call. Every other subprocess here bounds itself
# (`frontmost._run` at 5s, the lsof sweeps at 30s) and this one now runs on the
# follow pump, where an unbounded hang stops every instance's reaping without
# raising anything for the pump's own fault handler to report.
OPEN_TIMEOUT_SECONDS = 5.0

# Set to turn every deep link off. The raise is the app's own behaviour and
# cannot be declined, so the only complete answer is not to fire the link --
# which costs the threads it would have surfaced: they stay unreachable over
# IPC, and the rollout is the tier that still works. Callers are told that
# rather than handed something that looks like a focus which worked.
SUPPRESS_FOCUS_ENV = "CODEX_PILOT_SUPPRESS_FOCUS"


# Set to any other value to suppress. These spellings read as off, because
# `CODEX_PILOT_SUPPRESS_FOCUS=0` meaning *on* is the classic footgun, and
# someone reaching for this switch is reaching for it in a hurry.
_OFF = {"", "0", "false", "no", "off"}


def focus_suppressed() -> bool:
    return os.environ.get(SUPPRESS_FOCUS_ENV, "").strip().lower() not in _OFF


class FiredLink(NamedTuple):
    """One `open` invocation: the url it carried, and how it went.

    `returncode` travels with the url because the two are only meaningful
    together -- a url that was handed to a bundle which then refused it is not
    a thread that was surfaced.
    """

    url: str
    returncode: int
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class LinkTarget(NamedTuple):
    """A bundle to aim a deep link at, and whether it is already running.

    The two travel together because the second is only knowable at the moment
    the first is resolved -- both come out of one `serving_app` probe -- and
    separating them would mean probing twice or guessing.
    """

    path: Path
    live: bool


class ActionError(Exception):
    """An action could not be carried out."""


class NoOwnerError(ActionError):
    """No app window owns this thread, so it cannot be driven over IPC.

    Either the thread is not open in the app (drive it detached instead), or a
    Codex Desktop update bumped a pinned protocol version -- the two are
    indistinguishable from the router's reply alone, so callers should check
    whether a writer lock is held before concluding the thread is free.
    """


class UnclaimedThreadError(ActionError):
    """A writer lock is held, but no window will answer for the thread.

    Holding the lock and claiming ownership are different states, which is not
    obvious: the app holds a writer lock on every thread it has open -- including
    subagent threads and ones it is no longer rendering -- but only answers
    owner discovery for a thread a window is actually showing. Verified by
    probing: of 12 lock-holding threads, only 5 answered, and an unanswered one
    started answering as soon as it was opened in the app.

    So this is neither "free to resume" (the lock is held) nor a protocol
    problem. Bringing the thread forward in the app fixes it -- `focus_thread`
    does that. Version drift produces the same symptom, so it stays a secondary
    suspect if focusing does not help.

    Raised only when the holder is *known* to be the app. A lock held by
    another writer produces this same `no-client-found`, but focusing that
    thread would ask Codex Desktop to open a rollout somebody else is writing,
    so it gets `ForeignWriterError` and its own remedy instead.
    """


class ForeignWriterError(ActionError):
    """A writer that is not Codex Desktop holds this thread's lock.

    Neither route reaches it: the app cannot answer for a thread it does not
    hold, and resuming would put a second writer on the rollout. The only thing
    to do is wait for the holder to exit -- or, when it is one of our own runs,
    stop it. `_refuse_if_held_elsewhere` phrases both.
    """


class StalledTurnError(ActionError):
    """The app's newest turn has no id, so the app will not steer this thread.

    Its own type because the remedy is specific and nothing else in this class
    shares it: send a new message rather than wait, retry or focus. Callers
    branch on `type(exc).__name__`, which the MCP surface returns as `error`.
    """


class UnboundLinkError(ActionError):
    """The deep link cannot be aimed at the instance that owns the thread.

    `codex://` is claimed by every installed Codex bundle and LaunchServices
    resolves it to exactly one of them, so firing it unaimed hands the link to
    whichever app that happens to be. When that app's CODEX_HOME has no such
    thread, nothing happens and nothing says so -- the thread stays unmounted,
    which reads exactly like protocol drift. Refusing is the only honest
    alternative: there is no unaimed link worth firing.
    """


@dataclass(frozen=True)
class ResolvedThread:
    instance: Instance
    thread_id: str
    info: ThreadInfo

    @property
    def name(self) -> str | None:
        return self.info.name


class Session:
    """Holds one IPC connection per live instance, opened on demand."""

    def __init__(
        self,
        instances: list[Instance] | None = None,
        ipc_timeout: float = ipc_module.DEFAULT_TIMEOUT,
    ) -> None:
        self._instances = instances if instances is not None else discover_instances()
        self._ipc_timeout = ipc_timeout
        self._clients: dict[str, IpcClient] = {}
        self._stores: dict[str, ThreadStore] = {}
        self._runners: dict[str, DetachedRunner] = {}
        # Runs we spawned ourselves. Needed for two things nothing else can
        # tell us: that a lock holder is our own CLI rather than the app, and
        # that a thread we created still exists once it goes idle and drops
        # out of the lock listing.
        self._runs: dict[str, DetachedRun] = {}
        # Spawned, but their thread id had not appeared yet.
        self._untracked: list[DetachedRun] = []
        self._follow: dict[str, FollowManager] = {}
        self._pumps: dict[str, threading.Thread] = {}
        self._connecting: dict[str, threading.Lock] = {}
        self._stop = threading.Event()
        self._seq = SeqCounter()
        # Sequence numbers restart at 0 with the process, so a cursor from a
        # previous one silently filters out everything that follows it. The
        # epoch is what lets a caller notice instead of going quietly deaf.
        self._epoch = uuid.uuid4().hex
        self._guard = threading.RLock()
        # Reaping is serialized separately from `_guard`, which is held only in
        # short bursts. A reap now spans slow work -- surfacing the thread --
        # between claiming a finished run and announcing it, and two reapers
        # interleaving there would let one observe a run already claimed but not
        # yet announced, and report nothing for a turn that had in fact
        # finished. Each instance's pump reaps on a timer, and a smoke script or
        # a test can reap the same instance directly alongside it.
        #
        # Per instance, keyed like `_connecting`, because instances share no
        # state here: a reap filters to one slug and surfaces into one app. One
        # lock for all of them would let a slow `open` on one instance park
        # every other instance's pump -- which is where the follows are kept
        # alive, so they would go quiet without anything marking them lost.
        self._reap_guards: dict[str, threading.Lock] = {}
        self._snapshot_waiters: dict[str, list[queue.Queue[dict[str, Any]]]] = {}

    @property
    def instances(self) -> list[Instance]:
        return self._instances

    def store(self, instance: Instance) -> ThreadStore:
        with self._guard:
            if instance.slug not in self._stores:
                # The instance, not the store, knows every socket this app may
                # be listening on -- and getting that wrong misreads the app's
                # own writer as a foreign one.
                self._stores[instance.slug] = ThreadStore(
                    instance.codex_home, socket_candidates=instance.socket_candidates()
                )
            return self._stores[instance.slug]

    def follow_manager(self, instance: Instance) -> FollowManager:
        with self._guard:
            if instance.slug not in self._follow:
                self._follow[instance.slug] = FollowManager(instance.slug, seq=self._seq)
            return self._follow[instance.slug]

    def _ensure_pump(self, instance: Instance) -> None:
        """Drain this instance's broadcasts into its FollowManager.

        A background pump rather than draining on demand: events have to
        accumulate while nothing is polling, which is the entire point of a
        persistent follow.
        """
        with self._guard:
            existing = self._pumps.get(instance.slug)
            if existing is not None and existing.is_alive():
                return

            def pump() -> None:
                """Keep the connection alive and re-request snapshots after a gap.

                Frames reach the manager through the client's broadcast listener,
                so this loop exists for the two things a listener cannot do:
                notice the connection died, and re-subscribe a thread whose stream
                desynced. A gap is otherwise unrecoverable -- nothing would ever
                ask for a fresh snapshot and the follow would sit silent forever.
                """
                manager = self.follow_manager(instance)
                faults = 0
                while not self._stop.is_set():
                    try:
                        # First, and before anything that needs the app: a
                        # detached run finishing is news even when Codex Desktop
                        # is not running. Inside the guard, because a raise here
                        # used to end the thread outright.
                        self._adopt_late_ids(instance)
                        self._reap_runs(instance)
                        try:
                            # Reconnects are noticed inside client(), which queues
                            # the re-subscribe this loop then broadcasts.
                            client = self.client(instance)
                        except IpcError:
                            for thread_id in manager.followed:
                                manager.lost(thread_id, "Codex Desktop is not reachable")
                            # Detached runs do not need the app, so keep checking
                            # on them promptly even while it is unreachable.
                            self._stop.wait(0.5 if self._has_pending_runs(instance) else 5.0)
                            continue
                        # A request the app was not ready for is otherwise never
                        # repeated: the awaiting latch stops anything asking twice.
                        manager.stale_awaiting(RESYNC_RETRY_SECONDS)
                        pending = manager.take_resync_requests()
                        try:
                            for thread_id in pending:
                                client.broadcast(
                                    FOLLOWING_CHANGED, payloads.follow(thread_id, True)
                                )
                                client.broadcast(
                                    "thread-stream-following-status-requested",
                                    {"conversationId": thread_id, "hostId": "local"},
                                )
                        except IpcError:
                            # Already taken off the list; dropping them here
                            # means nothing ever asks for them again.
                            manager.requeue_resync(pending)
                            raise
                        faults = 0
                        if client.is_closed:
                            for thread_id in manager.followed:
                                manager.lost(thread_id, "IPC connection closed")
                            self._stop.wait(2.0)
                            continue
                    except IpcError:
                        # The app going away is ordinary, and the follows were
                        # already told why above.
                        self._stop.wait(2.0)
                        continue
                    except Exception as exc:  # noqa: BLE001 - see the comment
                        # Anything else is a defect here, or a frame shape we
                        # mishandle. The pump still must not die -- nothing
                        # restarts it, and every follow would stop being
                        # maintained with no way back -- but it must not hide
                        # either. A bare `continue` did exactly that once: a
                        # method that had gone missing raised on every tick
                        # while the follows looked healthy and quietly stopped
                        # being re-subscribed. So the reason goes where a caller
                        # already looks, and the retry slows down rather than
                        # spinning a core on a fault that will not clear itself.
                        faults += 1
                        reason = f"follow pump failed: {type(exc).__name__}: {exc}"
                        for thread_id in manager.followed:
                            manager.lost(thread_id, reason)
                        self._stop.wait(min(PUMP_FAULT_MAX_BACKOFF, 2.0 ** (faults - 1)))
                        continue
                    self._stop.wait(0.5)

            thread = threading.Thread(
                target=pump, name=f"codex-pilot-pump-{instance.slug}", daemon=True
            )
            self._pumps[instance.slug] = thread
            thread.start()

    def _has_pending_runs(self, instance: Instance) -> bool:
        with self._guard:
            return any(r.instance == instance.slug and not r.reported for r in self._runs.values())

    def _adopt_late_ids(self, instance: Instance) -> None:
        """Pick up a thread id that landed after start_thread stopped waiting.

        Otherwise a slow start leaves the run untracked for good: its lock is
        read as the app's, no completion is ever reported, and the child is
        never reaped.
        """
        with self._guard:
            pending = [
                r for r in self._untracked if r.instance == instance.slug and r.thread_id is None
            ]
        for run in pending:
            found = scan_for_thread_id(run.log_path)
            if found is None:
                if not run.running:
                    with self._guard:
                        if run in self._untracked:
                            self._untracked.remove(run)
                continue
            run.thread_id = found
            with self._guard:
                if run in self._untracked:
                    self._untracked.remove(run)
            self._register_run(run)

    def _reap_runs(self, instance: Instance) -> None:
        """Turn each finished detached run into a `turn_completed`, once.

        Polling the child is also what reaps it, so this doubles as the reaper.
        Serialized, because claiming a run and announcing it are no longer
        adjacent: surfacing the thread happens between them.
        """
        with self._reap_guard(instance):
            self._reap_locked(instance)

    def _reap_guard(self, instance: Instance) -> threading.Lock:
        with self._guard:
            if instance.slug not in self._reap_guards:
                self._reap_guards[instance.slug] = threading.Lock()
            return self._reap_guards[instance.slug]

    def _reap_locked(self, instance: Instance) -> None:
        finished: list[tuple[str, DetachedRun]] = []
        with self._guard:
            for thread_id, run in self._runs.items():
                if run.instance != instance.slug or run.reported or run.running:
                    continue
                run.reported = True
                finished.append((thread_id, run))
        if not finished:
            return
        try:
            surfaced = self._surface_finished(instance, finished)
        except Exception as exc:  # noqa: BLE001 - see the comment
            # Surfacing is best-effort; the completion event is not. These runs
            # are already marked reported, so anything escaping here would lose
            # the only announcement that they finished -- a turn silently never
            # completing, which is worse than any failure to raise a window.
            surfaced = {
                thread_id: self._not_surfaced("error", f"{type(exc).__name__}: {exc}")
                for thread_id, _ in finished
            }
        manager = self.follow_manager(instance)
        for thread_id, run in finished:
            code = run.returncode
            manager.emit_external(
                thread_id,
                EVENT_TURN_COMPLETED if code == 0 else EVENT_RUN_FAILED,
                {
                    "route": "detached",
                    "returncode": code,
                    "pid": run.pid,
                    "log_path": str(run.log_path),
                    "stopped": run.stopped,
                    # Whether the thread is now in the app, and why not when it
                    # is not. Reported rather than implied: a caller told only
                    # that the turn finished would have no way to tell a thread
                    # waiting on screen from one that stayed invisible.
                    #
                    # Named for the attempt, not the outcome, so that `surfaced`
                    # means the same thing everywhere: a bool here and a bool on
                    # `focus_thread`. One name carrying a bool in one place and
                    # an object in another is the kind of collision an agent
                    # reads straight past -- a non-empty dict is truthy, so
                    # `if result["surfaced"]` would call a declined surface a
                    # success.
                    "surfacing": surfaced[thread_id],
                },
            )

    def _surface_finished(
        self, instance: Instance, finished: list[tuple[str, DetachedRun]]
    ) -> dict[str, dict[str, Any]]:
        """Bring each just-finished thread forward, so the app can show it.

        This is the moment a detached thread becomes visible at all. While the
        run holds the writer lock the thread is reachable by neither route and
        no window renders it, so there is nothing to surface and focusing it
        would be the two-writer case. The instant the child exits the lock is
        free and the thread is an ordinary one the app can open -- which makes
        completion the first point where surfacing is both possible and safe.

        No settle before firing. The child's exit is what closes and flushes the
        rollout, so by the time `poll()` has an answer the file the app is about
        to read is already complete.

        One guard around the whole batch, as in `sync_threads`: three runs
        finishing together should cost the user one flash, not three.

        A dead app is left dead. The link would cold-start Codex to display a
        thread nobody asked to see, which is a heavier interruption than the one
        it was meant to save -- remodex declines the same launch for the same
        reason (`docs/protocol.md`). The result is on disk either way, and
        `read_thread` needs no window. A run that was *stopped* is not surfaced
        either: it did not finish, and whoever cancelled it did not ask to be
        shown it.

        Nothing here raises. It runs inside the pump, where an exception used to
        end the thread outright, and a link that could not be fired is no reason
        to lose the completion event it travels with.
        """
        out: dict[str, dict[str, Any]] = {}
        wanted = []
        for thread_id, run in finished:
            if not run.surface:
                out[thread_id] = self._not_surfaced(
                    "not_requested", "the dispatch asked for surface=false"
                )
            elif run.stopped:
                # A run we terminated did not finish, it was cancelled -- most
                # often to redispatch the slice. Raising the corpse into a
                # window is the opposite of what the caller just asked for.
                out[thread_id] = self._not_surfaced(
                    "stopped", "the run was stopped rather than finishing on its own"
                )
            else:
                wanted.append(thread_id)

        def decline(reason: str, detail: str) -> None:
            for thread_id in wanted:
                out[thread_id] = self._not_surfaced(reason, detail)
            wanted.clear()

        target: Path | None = None
        live = False
        if wanted and focus_suppressed():
            decline("suppressed", f"{SUPPRESS_FOCUS_ENV} is set; no link was fired")
        if wanted:
            try:
                aimed = self.link_target(instance)
            except UnboundLinkError as exc:
                decline("unaimable", str(exc))
            else:
                # `link_target` hands back a cold bundle rather than refusing,
                # because focusing is allowed to launch the app. Here it is not:
                # nothing asked to see this thread, and a cold start is a
                # heavier interruption than the one surfacing was meant to save.
                if aimed.live:
                    target, live = aimed.path, aimed.live
                else:
                    decline(
                        "app_not_running",
                        f"no app is serving instance {instance.slug!r}, so the thread was "
                        "left where it is rather than cold-starting Codex to show it -- "
                        "open the app and focus_thread it, or read_thread off the rollout",
                    )

        if target is None:
            # Every wanted thread was declined above -- `decline` empties the
            # list, so there is nothing left that could still be fired.
            return out

        pending: list[ResolvedThread] = []
        for thread_id in wanted:
            try:
                resolved = self.resolve(thread_id, instance.slug)
            except ThreadError as exc:
                # Per thread, like `sync_threads`: one thread the app cannot be
                # asked for is not a fact about the others finishing beside it.
                # Split from `refused` the way `sync_threads` splits it -- a
                # thread that vanished from the store and one the app must not
                # be asked for are different states with different remedies.
                out[thread_id] = self._not_surfaced("unresolvable", str(exc))
                continue
            except ActionError as exc:
                out[thread_id] = self._not_surfaced("refused", str(exc))
                continue
            try:
                self._refuse_unless_app_holds(resolved)
            except ActionError as exc:
                out[thread_id] = self._not_surfaced("refused", str(exc))
                continue
            pending.append(resolved)
        if not pending:
            return out

        # Per thread inside the shared guard, not per batch around it. A link
        # that failed says nothing about the ones fired beside it, and marking
        # the whole batch `link_failed` would report threads that really did
        # land as though they had not.
        failures: dict[str, str] = {}
        with self.frontmost_guard([target], live=live) as guard:
            for resolved in pending:
                try:
                    fired = self._open_thread_link(resolved, target)
                except (OSError, subprocess.SubprocessError) as exc:
                    failures[resolved.thread_id] = f"{type(exc).__name__}: {exc}"
                    continue
                if not fired.ok:
                    failures[resolved.thread_id] = (
                        f"`open` exited {fired.returncode}"
                        f"{f': {fired.stderr}' if fired.stderr else ''}"
                    )
        outcome = guard.outcome
        for resolved in pending:
            failure = failures.get(resolved.thread_id)
            if failure is not None:
                out[resolved.thread_id] = self._not_surfaced("link_failed", failure)
                continue
            out[resolved.thread_id] = {
                "surfaced": True,
                "reason": None,
                "detail": None,
                "app": str(target),
                "focus": outcome,
            }
        return out

    @staticmethod
    def _not_surfaced(reason: str, detail: str) -> dict[str, Any]:
        return {
            "surfaced": False,
            "reason": reason,
            "detail": detail,
            "app": None,
            "focus": None,
        }

    def follow_thread(
        self, ref: str, follow: bool = True, instance: str | None = None
    ) -> dict[str, Any]:
        """Start or stop a persistent follow.

        Only works while the app has the thread mounted -- an unmounted thread
        sends no stream state at all, so a follow on one is silently empty.
        """
        resolved = self.resolve(ref, instance)
        manager = self.follow_manager(resolved.instance)
        client = self.client(resolved.instance)
        client.broadcast(FOLLOWING_CHANGED, payloads.follow(resolved.thread_id, follow))
        if follow:
            manager.follow(resolved.thread_id)
            self._ensure_pump(resolved.instance)
        else:
            manager.unfollow(resolved.thread_id)
        return {
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "name": resolved.name,
            "following": follow,
            "followed": manager.followed,
        }

    def collect_events(
        self,
        threads: list[str] | None = None,
        after: int = 0,
        wait_seconds: float = 0.0,
        instance: str | None = None,
        epoch: str | None = None,
    ) -> dict[str, Any]:
        """Drain events across instances, waiting if asked.

        Sequence numbers are shared across managers, so one cursor is meaningful
        for all of them. Waiting is done by re-polling to a deadline rather than
        blocking on a single manager, or a second instance's events would only
        surface once the first happened to produce something.
        """
        if instance is not None and not any(i.slug == instance for i in self._instances):
            known = ", ".join(i.slug for i in self._instances)
            raise UnknownThreadError(f"no instance named {instance!r}; known: {known}")

        managers = [
            self.follow_manager(i)
            for i in self._instances
            if instance is None or i.slug == instance
        ]
        # A cursor we could never have issued belongs to a previous process, so
        # honouring it would drop every event from here on. Start over and say so.
        cursor_reset = (epoch is not None and epoch != self._epoch) or after > self._seq.peek()
        if cursor_reset:
            after = 0

        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            merged: list[dict[str, Any]] = []
            dropped = 0
            following: list[str] = []
            health: dict[str, Any] = {}
            for manager in managers:
                got = manager.collect(threads, after=after, wait_seconds=0.0)
                merged.extend(got["events"])
                dropped += got["dropped"]
                following.extend(got["following"])
                # Managers only answer for threads they track, so these cannot
                # collide: one instance's truth can no longer be overwritten by
                # another instance's ignorance.
                health.update(got["threads"])
            if merged or time.monotonic() >= deadline:
                merged.sort(key=lambda e: e["seq"])
                # Anything the caller asked about that no instance follows is
                # genuinely unfollowed -- said once, here, rather than by every
                # manager that has never heard of it.
                for thread_id in threads or []:
                    health.setdefault(
                        thread_id,
                        {
                            "health": "not_following",
                            "reason": None,
                            "pending": [],
                            "pending_known": False,
                        },
                    )
                return {
                    "events": merged,
                    "cursor": merged[-1]["seq"] if merged else after,
                    "dropped": dropped,
                    "following": sorted(set(following)),
                    "threads": health,
                    "epoch": self._epoch,
                    "cursor_reset": cursor_reset,
                }
            self._stop.wait(min(0.25, max(0.05, deadline - time.monotonic())))

    def runner(self, instance: Instance) -> DetachedRunner:
        with self._guard:
            if instance.slug not in self._runners:
                self._runners[instance.slug] = DetachedRunner(instance, self.store(instance))
            return self._runners[instance.slug]

    def _connect_guard(self, instance: Instance) -> threading.Lock:
        """One connect at a time per instance, held without the cache lock."""
        with self._guard:
            if instance.slug not in self._connecting:
                self._connecting[instance.slug] = threading.Lock()
            return self._connecting[instance.slug]

    def client(self, instance: Instance) -> IpcClient:
        """The instance's live connection, opening one if it needs to.

        Two locks rather than one, deliberately. `_guard` protects the caches
        and is taken only for the lookups; a per-instance connect lock
        serialises the handshake, so the follow pump and an MCP call cannot both
        build a client and leave one orphaned and unread. Holding `_guard`
        across `initialize()` would do the same job and cost far more: an app
        that accepts the socket and then says nothing freezes every unrelated
        caller -- thread stores, follow managers, collect_events -- for the full
        IPC timeout, turning one wedged connection into a wedged session.
        """
        with self._connect_guard(instance):
            with self._guard:
                if self._stop.is_set():
                    raise IpcUnavailable("session is closed")
                existing = self._clients.get(instance.slug)
                socket_path = instance.socket_path()
                if existing is not None and not existing.is_closed:
                    if socket_path is not None and existing.matches_socket():
                        return existing
                    # Same path, different inode (or none): the app re-bound the
                    # socket, so this connection points at a server that is gone.
                    # It would never report itself closed -- nothing arrives on it
                    # to trip the reader -- so retire it explicitly.
                    existing.close()
                    self._clients.pop(instance.slug, None)
            if socket_path is None:
                raise IpcUnavailable(
                    f"Codex Desktop instance {instance.slug!r} is not running "
                    f"(no socket under {instance.codex_home})"
                )
            client = IpcClient(socket_path=socket_path, timeout=self._ipc_timeout)
            try:
                client.initialize()
            except BaseException:
                # A half-built client still owns a socket and a reader thread.
                # The pump retries every few seconds, so leaking one per attempt
                # exhausts both while the app is down -- which is precisely when
                # the retries happen.
                client.close()
                raise
            client.add_broadcast_listener(self._make_listener(instance))
            with self._guard:
                if self._stop.is_set():
                    client.close()
                    raise IpcUnavailable("session is closed")
                self._clients[instance.slug] = client
            if existing is not None:
                # Replacing a client, not opening the first one: the app tracks
                # stream subscriptions per connection, so every follow that was
                # live on the old one is gone. This belongs here rather than in
                # the pump because this is the only place that knows a
                # replacement happened -- the pump cannot tell whether the
                # client it picks up is the one the subscription was made on,
                # and a drop between subscribing and its first tick would
                # otherwise go unnoticed forever.
                self.follow_manager(instance).resync_all("ipc reconnected")
            return client

    def _rearm_follows(self, instance: Instance) -> None:
        """Re-subscribe every followed thread after a new connection is made.

        A follow is a broadcast the app records against the *connection* it
        arrived on, while our registration lives on the Session. So a reconnect
        leaves a thread listed as followed while no frames will ever arrive for
        it again. Nothing detected that before: the pump only re-subscribes
        threads that asked for a resync, and losing a connection never asked.
        """
        manager = self._follow.get(instance.slug)
        if manager is None or not manager.followed:
            return
        manager.resync_all("reconnected")
        self._ensure_pump(instance)

    def _make_listener(self, instance: Instance) -> Any:
        """Fan stream-state frames to the follow manager and to any waiter.

        Both need the same frames. Draining a shared queue instead would let
        whichever consumer polls first swallow events the other was waiting for.
        """

        def listener(frame: dict[str, Any]) -> None:
            if frame.get("method") != STREAM_STATE_CHANGED:
                return
            thread_id = (frame.get("params") or {}).get("conversationId")
            manager = self.follow_manager(instance)
            if manager.is_following(str(thread_id)):
                manager.handle_frame(frame)
            with self._guard:
                waiters = list(self._snapshot_waiters.get(str(thread_id), []))
            for waiter in waiters:
                with contextlib.suppress(queue.Full):
                    waiter.put_nowait(frame)

        return listener

    def close(self) -> None:
        self._stop.set()
        for pump in list(self._pumps.values()):
            pump.join(timeout=3.0)
        self._pumps.clear()
        with self._guard:
            for client in self._clients.values():
                client.close()
            self._clients.clear()

    # -- resolution ---------------------------------------------------------

    def resolve(self, ref: str, instance: str | None = None) -> ResolvedThread:
        """Bind a thread reference to exactly one (instance, thread_id).

        Searching every instance is what makes a bare name usable, but it means
        a name present in two instances is genuinely ambiguous -- refuse rather
        than pick, and let the caller disambiguate with `instance`.
        """
        candidates = [i for i in self._instances if instance is None or i.slug == instance]
        if not candidates:
            known = ", ".join(i.slug for i in self._instances)
            raise UnknownThreadError(f"no instance named {instance!r}; known: {known}")

        hits: list[tuple[Instance, str]] = []
        for inst in candidates:
            try:
                hits.append((inst, self.store(inst).resolve(ref)))
            except (UnknownThreadError, AmbiguousThreadError):
                continue

        if not hits:
            scope = f"instance {instance!r}" if instance else "any instance"
            raise UnknownThreadError(f"no thread matching {ref!r} in {scope}")
        if len(hits) > 1:
            listing = ", ".join(f"{i.slug}:{t}" for i, t in hits)
            raise AmbiguousThreadError(
                f"{ref!r} matches threads in several instances ({listing}); "
                "pass `instance` to choose one"
            )
        inst, thread_id = hits[0]
        return ResolvedThread(inst, thread_id, self.store(inst).describe(thread_id))

    # -- owner --------------------------------------------------------------

    def owner_of(self, resolved: ResolvedThread, timeout: float | None = None) -> str:
        """The clientId of the window that owns this thread.

        `timeout` bounds the wait below the router's own ~10s discovery answer,
        for a caller that only wants the cheap positive. Silence then means "did
        not answer in time", not "the connection is dead", so it is exempt from
        the strike counter.
        """
        client = self.client(resolved.instance)
        try:
            response = client.request(
                OWNER_DISCOVERY,
                payloads.owner_discovery(resolved.thread_id),
                timeout=timeout,
                silence_counts=timeout is None,
            )
        except RouterError as exc:
            if exc.error == "no-client-found":
                # Same reply, three different situations. Which one it is
                # depends on who holds the writer lock, and the remedies do not
                # overlap: focusing a thread another writer holds is the one
                # thing that must never be suggested.
                if resolved.info.app_owned:
                    raise UnclaimedThreadError(
                        f"{resolved.thread_id} holds a writer lock ({resolved.info.holder}) but "
                        "no window claims it: the app has the thread open without showing it. "
                        "Bring it forward with focus_thread and retry. It cannot be resumed "
                        "detached either, because the lock is taken. If focusing does not help, "
                        "suspect version drift and run scripts/extract_registry.py --check."
                    ) from exc
                if resolved.info.holder is not None:
                    raise UnclaimedThreadError(
                        f"{resolved.thread_id} holds a writer lock "
                        f"({resolved.info.holder.described}) and no window claims it. Do not "
                        "focus it until you know the holder is the app: if it is not, that "
                        "puts a second writer on the rollout. Check the holder's pid "
                        f"(`ps -o command= -p {resolved.info.holder.pid}`) and wait for it "
                        "to exit; read_thread works meanwhile."
                    ) from exc
                if not resolved.info.lock_known:
                    raise UnclaimedThreadError(
                        f"no window owns {resolved.thread_id}, and the writer lock could not "
                        "be probed, so whether anything holds it is unknown. Do not resume it "
                        "detached on that basis. Check `lsof` is usable and retry."
                    ) from exc
                raise NoOwnerError(
                    f"no window owns {resolved.thread_id} in instance "
                    f"{resolved.instance.slug!r}; it is not open in the app"
                ) from exc
            raise
        owner = response.get("handledByClientId")
        if not isinstance(owner, str):
            raise ActionError(f"owner discovery returned no client id: {response}")
        return owner

    def probe_mounted(self, resolved: ResolvedThread, timeout: float | None = None) -> str | None:
        """The owning client id if the app is mounted on this thread, else None.

        `None` is "not provably mounted", which is weaker than "unmounted": an
        unreachable router, a refusal, or -- with `timeout` set -- an answer
        that did not arrive in time all land here alongside a genuine
        no-client-found. Callers must treat it as absence of proof and take the
        safe branch, never as proof of absence.

        The probe is lopsided. A mounted thread answers in ~0.4s; an unmounted
        one costs the router's full ~10s discovery timeout before it says so.
        A caller that only wants the cheap positive passes a `timeout`.
        """
        try:
            return self.owner_of(resolved, timeout=timeout)
        except (UnclaimedThreadError, NoOwnerError):
            return None
        except (ActionError, IpcError):
            return None

    def census(
        self,
        threads: list[str] | None = None,
        instance: str | None = None,
        workers: int = CENSUS_WORKERS,
    ) -> dict[str, Any]:
        """Which threads the app will actually answer for, right now.

        Holding a writer lock and being reachable are different states, and only
        a probe distinguishes them. Mounting is additive rather than exclusive:
        several threads answer at once, and mounting another does not evict
        them, so this is a census of a set and not of a single visible thread.
        """
        rows = self.list_threads(instance)
        if threads:
            wanted = {t for t in threads}
            rows = [r for r in rows if r["thread"] in wanted]

        targets: list[tuple[dict[str, Any], ResolvedThread | None]] = []
        for row in rows:
            try:
                targets.append((row, self.resolve(row["thread"], row["instance"])))
            except ThreadError:
                targets.append((row, None))

        results: dict[str, str | None] = {}
        probeable = [(r, res) for r, res in targets if res is not None and r["route"] == "desktop"]
        if probeable:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, min(workers, len(probeable)))
            ) as pool:
                futures = {
                    pool.submit(self.probe_mounted, res): row["thread"] for row, res in probeable
                }
                for future in concurrent.futures.as_completed(futures):
                    results[futures[future]] = future.result()

        out = []
        for row, _ in targets:
            owner = results.get(row["thread"])
            entry = dict(row)
            entry["mounted"] = owner is not None
            entry["owner"] = owner
            if row["route"] != "desktop":
                # Only an app-owned thread can be mounted; the rest are not
                # silent, they are simply not the app's to answer for.
                entry["mounted"] = False
                entry["owner"] = None
            out.append(entry)
        mounted = [e["thread"] for e in out if e["mounted"]]
        return {
            "threads": out,
            "mounted": mounted,
            "unmounted": [e["thread"] for e in out if not e["mounted"] and e["route"] == "desktop"],
            "clients": sorted({e["owner"] for e in out if e["owner"]}),
        }

    def sync_threads(
        self,
        threads: list[str] | None = None,
        mount: bool = False,
        instance: str | None = None,
        settle_seconds: float = 6.0,
    ) -> dict[str, Any]:
        """Census, and optionally bring unmounted threads forward so they stream.

        A follow only streams while the app has the thread mounted, so a thread
        it holds without rendering reports nothing at all. Focusing is additive
        (measured: mounting a fifth evicted none of four), which makes this a
        one-off warm-up rather than a rotation -- do not cycle through threads,
        mount the ones you intend to watch and leave them mounted.

        Threads being written by one of our own detached runs are skipped: they
        are not the app's to mount, and focusing one would put a second writer
        on the rollout.

        Nothing a single thread does aborts the sweep. A thread can be listed
        and still be unmountable -- a lock fd is enough for `list_threads` to
        report it, while `resolve` needs a rollout or an index entry, which the
        app has not necessarily written yet -- the holder can change between the
        census and the mount, turning a `desktop` row into somebody else's, and
        an instance whose serving app cannot be named has no link to aim. None
        of those is a fact about the other threads, so none of them costs those
        threads: each comes back in `skipped` with a reason rather than being
        raised over the ones that would have mounted, or dropped as if it had
        never been listed. Several are transient, and the next sweep may well
        mount what this one could not.
        """
        before = self.census(threads, instance)
        if mount and before["unmounted"] and focus_suppressed():
            # The census is still the honest half of the answer; what is
            # withheld is the mounting, and the caller is told which.
            return {
                **before,
                "focused": [],
                "mounted_by_sync": [],
                "focus": {"restored": False, "reason": "suppressed"},
                # Not classified any further: the per-thread checks below never
                # ran, so this says the sweep declined to mount them, not that
                # they were mountable. One of these may also be held by another
                # writer, which an unsuppressed sweep would have said instead.
                "skipped": [
                    self._skip(row, "suppressed", f"{SUPPRESS_FOCUS_ENV} is set; not attempted")
                    for row in before["threads"]
                    if not row["mounted"] and row["route"] == ROUTE_DESKTOP
                ],
            }
        if not mount or not before["unmounted"]:
            return {**before, "focused": [], "mounted_by_sync": [], "focus": None, "skipped": []}

        pending: list[tuple[ResolvedThread, Path]] = []
        skipped: list[dict[str, Any]] = []
        # One probe per instance, not per thread: aiming a link costs an lsof
        # and a ps sweep, and every thread in a sweep shares its instance's
        # answer.
        targets: dict[str, Path] = {}
        # One cold app in the sweep sets the pace for the batch: the guard is
        # per batch, so it has to outlast the slowest window it is waiting on.
        all_live = True
        # Rows rather than `unmounted`, because an id alone has lost its
        # instance: thread ids are unique within a CODEX_HOME and not across
        # them, so re-resolving bare could bind the wrong app -- or refuse as
        # ambiguous -- for an id two instances happen to share.
        for row in before["threads"]:
            if row["mounted"] or row["route"] != ROUTE_DESKTOP:
                continue
            try:
                resolved = self.resolve(row["thread"], row["instance"])
            except ThreadError as exc:
                skipped.append(self._skip(row, "unresolvable", str(exc)))
                continue
            if self.live_run(resolved.thread_id) is not None:
                # `route_for` consults the run map first, so this only catches a
                # run of ours that took the lock since the census -- which is
                # exactly when it matters.
                skipped.append(
                    self._skip(
                        row,
                        ROUTE_RUNNING,
                        "one of our own detached runs took the writer lock since the census; "
                        "it is not the app's to mount until that run exits",
                    )
                )
                continue
            try:
                self._refuse_unless_app_holds(resolved)
            except ActionError as exc:
                skipped.append(self._skip(row, "refused", str(exc)))
                continue
            slug = resolved.instance.slug
            if slug not in targets:
                try:
                    aimed = self.link_target(resolved.instance)
                    targets[slug] = aimed.path
                    all_live = all_live and aimed.live
                except UnboundLinkError as exc:
                    # Per instance rather than per thread, but skipped the same
                    # way: a sweep can span instances, and one whose app cannot
                    # be named is no reason to leave the others unmounted.
                    skipped.append(self._skip(row, "unaimable", str(exc)))
                    continue
            pending.append((resolved, targets[slug]))
        if not pending:
            return {
                **before,
                "focused": [],
                "mounted_by_sync": [],
                "focus": None,
                "skipped": skipped,
            }

        # One guard around the sweep: each link raises the app, and the user
        # should be put back once at the end rather than fought over per thread.
        # A sweep can span instances, so the guard watches every bundle it is
        # about to raise rather than assuming there is only one.
        with self.frontmost_guard({target for _, target in pending}, live=all_live) as guard:
            for resolved, target in pending:
                self._open_thread_link(resolved, target)
        # Settle *after* the guard, not inside it: the app mounts on its own
        # time, and the user should not spend that time in a window they did
        # not ask for.
        self._stop.wait(settle_seconds)
        focused = [r.thread_id for r, _ in pending]
        after = self.census(threads, instance)
        gained = sorted(set(after["mounted"]) - set(before["mounted"]))
        return {
            **after,
            "focused": focused,
            "mounted_by_sync": gained,
            "focus": guard.outcome,
            "skipped": skipped,
        }

    @staticmethod
    def _skip(row: dict[str, Any], reason: str, detail: str) -> dict[str, Any]:
        """One thread the sweep passed over, said in full.

        Both halves matter: the tag is what a caller can branch on, and the
        message is the only place the particular reason survives -- a skip
        reported without it is barely better than the silent drop.
        """
        return {
            "instance": row["instance"],
            "thread": row["thread"],
            "reason": reason,
            "detail": detail,
        }

    def focus_thread(
        self, ref: str, instance: str | None = None, activate: bool = False
    ) -> dict[str, Any]:
        """Make a window claim a thread, so the app will answer for it.

        The app answers owner discovery only for a thread it is rendering, so a
        thread it holds open in the background is undriveable until something
        surfaces it. The `codex://threads/<id>` deep link is how the app itself
        navigates, and it takes effect in a couple of seconds.

        This is not quiet. Handling the deep link runs the app's own
        `ensurePrimaryWindowVisible`, which restores, shows and focuses the
        primary window before navigating, so Codex comes to the front either
        way. `-g` still buys the smaller half -- it keeps macOS from activating
        the app on the launch side -- so it stays the default, and
        `activate=True` is for a caller that wants the whole thing.

        Since the raise cannot be declined, it is undone instead: `frontmost_guard`
        hands the screen back to whoever had it. That makes this a flash rather
        than a theft, and a flash is still an interruption -- what actually
        spares the user is calling this less: focus to drive a thread, not to
        look at one.
        """
        resolved = self.resolve(ref, instance)
        self._refuse_unless_app_holds(resolved)
        aimed = self.link_target(resolved.instance)
        target = aimed.path
        owner = None if activate else self.probe_mounted(resolved, FOCUS_PROBE_TIMEOUT_SECONDS)
        if owner is None and focus_suppressed():
            # Checked *after* the probe on purpose. Reporting an unreachable
            # thread without looking would be asserting the absence rather than
            # defaulting to it, and a thread the app already shows is reachable
            # whatever this setting says -- that case falls through below and is
            # reported as the skip it is.
            return {
                "instance": resolved.instance.slug,
                "app": str(target),
                "thread": resolved.thread_id,
                "name": resolved.name,
                "owner": None,
                "opened": None,
                "activated": False,
                "surfaced": False,
                "focus": {"restored": False, "reason": "suppressed"},
                "note": (
                    f"{SUPPRESS_FOCUS_ENV} is set, so nothing was surfaced and nothing "
                    "proved this thread already mounted: treat it as unreachable over IPC "
                    "and read it with read_thread, which works off the rollout and needs "
                    "no window."
                ),
            }
        if owner is not None:
            # Already rendering it: the link would re-run the app's window
            # restore for a screen that needs no changing, which is the whole
            # cost of a focus and none of its effect.
            return {
                "instance": resolved.instance.slug,
                "app": str(target),
                "thread": resolved.thread_id,
                "name": resolved.name,
                "owner": owner,
                "opened": None,
                "activated": False,
                "surfaced": True,
                "focus": {"restored": False, "reason": "skipped_already_mounted"},
                "note": "already mounted; the app answers for this thread now",
            }
        with self.frontmost_guard([target], live=aimed.live) as guard:
            url = self._open_thread_link(resolved, target, activate).url
        return {
            "instance": resolved.instance.slug,
            "app": str(target),
            "thread": resolved.thread_id,
            "name": resolved.name,
            # Not proof it was unmounted -- only that nothing proved otherwise
            # inside the probe's second. See `probe_mounted`.
            "owner": None,
            "opened": url,
            "activated": activate,
            "surfaced": True,
            "focus": guard.outcome,
            "note": "give the app a moment, then retry the call that failed",
        }

    def _refuse_unless_app_holds(self, resolved: ResolvedThread) -> None:
        """Every precondition for firing a deep link, in one place.

        Stricter than the shared guard, which lets an unclassifiable holder
        through so the IPC verbs can at least try. Focusing is the one verb
        with no diagnostic value in trying: it asks the app to open a rollout
        whose writer we could not identify. The app would refuse the lock
        itself, but this is the two-writer direction and it is not worth being
        one bug away from.

        `sync_threads` reaches the same conclusion by a different road -- it
        only ever focuses threads `census` already routed as `desktop` -- and
        calls this anyway, so the invariant has one enforcement point rather
        than two that can drift apart.
        """
        self._refuse_if_held_elsewhere(resolved)
        if not resolved.info.lock_known:
            # `holder is None` proves nothing here -- `ThreadInfo.lock_known`
            # says so itself. `route_for` already refuses to call this state
            # `detached`, and the deep link is the mirror of that route: it asks
            # the app to take the lock. Surfacing now runs unattended on every
            # detached completion, so an unreadable probe must not be the thing
            # that decides a rollout gets a second writer.
            raise ForeignWriterError(
                f"the writer lock on {resolved.thread_id} could not be probed, so whether "
                "anything holds it is unknown -- refusing to ask the app to open it rather "
                "than risk a second writer on the rollout. Check that `lsof` is usable and "
                "retry; `read_thread` works off the rollout meanwhile."
            )
        holder = resolved.info.holder
        if holder is not None and holder.is_app is None:
            raise ForeignWriterError(
                f"{holder.described} holds the writer lock on {resolved.thread_id}. Until "
                "the holder is known to be Codex Desktop, focusing it risks asking the app "
                "to open a rollout another writer has. Check the pid "
                f"(`ps -o command= -p {holder.pid}`) and retry once `lsof` is usable."
            )

    def link_target(self, instance: Instance) -> LinkTarget:
        """The bundle this instance's deep links must be handed to, and its state.

        `live` says whether an app is currently serving the instance's socket.
        It decides nothing about *where* the link goes -- both branches below
        return a real bundle -- only how long the caller should expect to wait
        for the window, since a cold bundle has to start before it can rise.

        Aiming the link is not a refinement, it is the difference between the
        link working and doing nothing. `codex://` is claimed by every Codex
        bundle installed, LaunchServices resolves it to one of them, and a
        thread that lives in another instance's CODEX_HOME lands in an app that
        has never heard of it. `open -a <bundle>` overrides that binding, and
        that it delivers a `codex://` URL to an app which is *not* the scheme's
        handler is the fact the whole approach rests on -- measured against two
        live apps on 2026-08-28, see `docs/protocol.md`.

        Whichever app is *serving the instance's socket* is the target, not
        `Instance.app_path`: that is whichever stamped bundle claimed the
        CODEX_HOME, and two bundles can claim one. On this machine it names
        `ChatGPT Veridue.app` for the default instance while
        `/Applications/ChatGPT.app` is the app actually serving it -- opening
        the first would ask a second app for a rollout the first one holds.

        When nothing is listening there is no such app to ask, and the link
        still has to name one: focusing a thread nothing holds is allowed, and
        it is the deep link that launches the app in the first place. A cold
        home has no first writer to collide with, so any bundle *stamped* with
        that CODEX_HOME serves it safely -- the stock bundle for the default
        home, since a clone may share it, and `app_path` otherwise.

        Anything short of a bundle raises. There is no fallback worth having:
        the unaimed link is precisely the bug.
        """
        found = serving_app(instance.socket_candidates())
        if found.bundle is not None:
            return LinkTarget(found.bundle, live=True)
        if not found.known:
            raise UnboundLinkError(
                f"could not tell which app is serving instance {instance.slug!r} "
                f"({instance.codex_home}), so the deep link cannot be aimed at it. "
                "Firing it unaimed would hand the thread to whichever app "
                "LaunchServices resolves `codex://` to, and a wrong one fails "
                "silently. Retry once `lsof` and `ps` are usable."
            )
        cold = stock_app() if instance.is_default else None
        cold = cold or instance.app_path
        if cold is not None:
            return LinkTarget(cold, live=False)
        raise UnboundLinkError(
            f"no app is listening on instance {instance.slug!r}'s socket "
            f"({instance.codex_home}) and no installed bundle is known to serve it, so "
            "there is nothing to aim the link at. Launch that instance's Codex Desktop "
            "and retry."
        )

    def _open_thread_link(
        self, resolved: ResolvedThread, target: Path, activate: bool = False
    ) -> FiredLink:
        """Fire the deep link and nothing else, so a batch can share one guard.

        The target comes in rather than being resolved here so that a sweep can
        aim every thread of one instance from a single probe, and so that the
        guard can be told which bundles are about to rise.

        The exit status comes back rather than being dropped. `open` failing --
        a bundle deleted while its process still serves the socket, or a
        LaunchServices refusal -- is knowledge this process already has, and
        discarding it would leave a caller to infer success from the mere
        absence of an exception. That is the house rule, in the one field this
        reports through.

        Bounded like every other subprocess here (`frontmost._run` at 5s, the
        lsof sweeps at 30s). Unbounded it can wedge the follow pump, which now
        fires this on every detached completion rather than only on request.
        """
        url = f"codex://threads/{resolved.thread_id}"
        background = [] if activate else ["-g"]
        argv = [frontmost.OPEN, *background, "-a", str(target), url]
        done = subprocess.run(argv, check=False, capture_output=True, timeout=OPEN_TIMEOUT_SECONDS)
        stderr = done.stderr.decode(errors="replace").strip() if done.stderr else ""
        return FiredLink(url=url, returncode=done.returncode, stderr=stderr)

    def frontmost_guard(self, targets: Iterable[Path], *, live: bool) -> frontmost.FrontmostGuard:
        """Give the user's foreground app back after the app raises itself.

        Handling a `codex://` link raises Codex Desktop's window before it
        navigates, so surfacing a thread always interrupts whoever is at the
        keyboard. The guard puts them back. It is deliberately per *batch*:
        mounting five threads should cost one flash, not five.

        It is told exactly which bundles are about to rise, which is only
        possible because the link is aimed: an `open -a` names the app that
        will come forward, so the raise can be attributed to the one bundle
        that caused it. Watching every installed Codex bundle instead would
        misread a user sitting in a *different* instance as already being where
        we are sending them, and leave them there.

        A cold target gets the longer deadline: the link is about to launch the
        app, and a launch reaches the screen well after a raise would have.
        `live` is required rather than defaulted because the two deadlines fail
        differently: guessing warm on a cold app gives up before the window
        arrives and reports `not_raised` for a raise that did happen, which is
        the interruption nothing undoes. A caller has to have looked.
        """
        return frontmost.FrontmostGuard(
            targets,
            timeout=(
                frontmost.RAISE_TIMEOUT_SECONDS if live else frontmost.COLD_RAISE_TIMEOUT_SECONDS
            ),
        )

    # -- mutating verbs -----------------------------------------------------

    def _refuse_if_held_elsewhere(self, resolved: ResolvedThread) -> None:
        """Stop before any verb that assumes the app holds the lock.

        A held lock only means *someone* is writing. Since start_thread that
        someone can be one of our own `codex exec` children, and it can equally
        be a run another process started. Without this check `owner_of` reports
        no-client-found, the caller is told the app has the thread open without
        showing it, and the suggested remedy -- focus_thread -- asks Codex
        Desktop to open a thread another writer is still holding. That is the
        two-writer case the lock exists to prevent, reached by following our own
        error message.

        Two phrasings because there are two remedies. A run we started can be
        stopped and its log read; one we did not can only be waited out, and its
        pid is not ours to signal.
        """
        own = self.live_run(resolved.thread_id)
        if own is not None:
            raise ForeignWriterError(
                f"a detached run codex-pilot started (pid {own.pid}) still holds the writer "
                f"lock on {resolved.thread_id}, so it cannot be driven through the app. "
                f"Wait for it (collect_events reports turn_completed when it exits), read "
                f"{own.log_path}, or stop_turn to terminate it. Do not focus it in the app "
                "while it is running -- that would put a second writer on the rollout."
            )
        holder = resolved.info.holder
        if holder is None or holder.is_app is not False:
            return
        raise ForeignWriterError(
            f"{holder} holds the writer lock on {resolved.thread_id} and is not Codex "
            "Desktop -- another writer, most likely a `codex exec` run this process did "
            "not start. Neither route reaches the thread until it exits: the app cannot "
            "answer for a thread it does not hold, and resuming would put a second writer "
            "on the rollout. Wait for it to finish, and read the rollout (read_thread) "
            "for what it is doing meanwhile. Do not focus it in the app while it runs."
        )

    def _follower_request(
        self, resolved: ResolvedThread, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._refuse_if_held_elsewhere(resolved)
        owner = self.owner_of(resolved)
        client = self.client(resolved.instance)
        response = client.request(method, params, target_client_id=owner)
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def instance_for(self, slug: str | None) -> Instance:
        """Pick an instance by slug, defaulting to the primary one."""
        if slug is None:
            # discover_instances() sorts the default first.
            return self._instances[0]
        for inst in self._instances:
            if inst.slug == slug:
                return inst
        known = ", ".join(i.slug for i in self._instances)
        raise UnknownThreadError(f"no instance named {slug!r}; known: {known}")

    def _register_run(self, run: DetachedRun) -> None:
        """Track a run we spawned, replacing any earlier one for that thread.

        Replacing matters: a finished run left in place shadows a newer live one
        on the same thread, so an orchestrator reading `running` would conclude
        the work was done while a different process was still writing.

        Finished runs are kept on purpose -- they are how a thread we started
        stays listed once it drops its lock -- but only up to a bound, or a long
        orchestration session grows this map forever.
        """
        if run.thread_id is None:
            # Keep it in sight: the id may still show up in the log, and an
            # unwatched child is one nothing will ever reap or report.
            with self._guard:
                self._untracked.append(run)
            inst = next((i for i in self._instances if i.slug == run.instance), None)
            if inst is not None:
                self._ensure_pump(inst)
            return
        with self._guard:
            self._runs.pop(run.thread_id, None)
            self._runs[run.thread_id] = run
            if len(self._runs) > MAX_TRACKED_RUNS:
                finished = [t for t, r in self._runs.items() if not r.running]
                for thread_id in finished[: len(self._runs) - MAX_TRACKED_RUNS]:
                    del self._runs[thread_id]
        # The pump is what notices this run finishing and reports it, so a
        # detached spawn has to start it -- a caller may never call follow_thread.
        inst = next((i for i in self._instances if i.slug == run.instance), None)
        if inst is not None:
            self._ensure_pump(inst)

    def live_run(self, thread_id: str) -> DetachedRun | None:
        """Our own detached run on this thread, if one is still going."""
        with self._guard:
            run = self._runs.get(thread_id)
        return run if run is not None and run.running else None

    def route_for(self, info: ThreadInfo) -> str:
        """Which route this thread can be driven by right now.

        Four states, not two. A lock holder is not necessarily the app: a
        detached `codex exec` holds the lock too. Calling that 'desktop' sends
        callers to an IPC route that cannot work, and calling it 'detached'
        promises it is free to resume when it is not -- so it gets its own
        value, decided from the holder's pid rather than from anything only
        this process knows.

        The run map is consulted first because it is strictly more informative
        when it has an answer -- it knows the run is ours, and it covers the gap
        between spawning a child and that child taking the lock, where the
        process table would still say the thread is free.
        """
        if self.live_run(info.thread_id) is not None:
            return ROUTE_RUNNING
        if not info.lock_known:
            return ROUTE_UNKNOWN
        if info.holder is None:
            return ROUTE_DETACHED
        if info.holder.is_app is None:
            return ROUTE_UNKNOWN
        return ROUTE_DESKTOP if info.holder.is_app else ROUTE_RUNNING

    def start_thread(
        self,
        text: str,
        cwd: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        base: str | None = None,
        instance: str | None = None,
        sandbox: str | None = None,
        approval: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        surface: bool = True,
    ) -> dict[str, Any]:
        """Create a new thread and start its first turn, in a place you name.

        Returns as soon as the thread has an id; the agent keeps working in the
        background. Where it works is never inferred: either `cwd` names an
        existing directory, or `repo` plus `branch` makes a worktree for it,
        laid out the way Codex lays one out. A thread started from wherever the
        caller happened to be is the failure this exists to prevent.

        The thread is brought forward in Codex Desktop when the run exits, so
        finished work lands somewhere the user can see and carry on from. It
        cannot be shown any earlier than that: a running detached thread holds
        the writer lock, which is exactly what makes it unrenderable. Pass
        `surface=False` for a fan-out whose completions should stay silent.
        """
        from . import worktrees

        inst = self.instance_for(instance)
        worktree: worktrees.Worktree | None = None
        if branch or repo:
            if not (branch and repo):
                raise ActionError("making a worktree needs both `repo` and `branch`")
            if cwd:
                raise ActionError("pass `cwd`, or `repo` plus `branch`, not both")
            worktree = worktrees.create(
                Path(repo).expanduser(),
                branch,
                root=worktrees.default_root(inst.codex_home),
                base=base,
            )
            cwd = str(worktree.path)
        if not cwd:
            raise ActionError("start_thread needs `cwd`, or `repo` plus `branch`")

        run = self.runner(inst).start(
            text,
            cwd=Path(cwd).expanduser(),
            sandbox=sandbox or "workspace-write",
            approval=approval or "never",
            model=model,
            effort=effort,
            service_tier=service_tier,
        )
        run.surface = surface
        out = run.as_dict()
        if worktree is not None:
            out["worktree"] = worktree.as_dict()
        self._register_run(run)
        if run.thread_id is None:
            out["note"] = (
                "the run started but reported no thread id in time -- read log_path "
                "to see what happened; the thread may not have been created"
            )
        return out

    def send_message(
        self,
        ref: str,
        text: str,
        instance: str | None = None,
        sandbox: str | None = None,
        approval: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        surface: bool = True,
    ) -> dict[str, Any]:
        """Start a turn, by whichever route the thread's lock state permits.

        A thread the app has open can only be driven over IPC; one nothing holds
        can only be driven detached. The writer lock decides, and it is never
        worked around -- if the app owns the thread but no window claims it,
        that is drift, and running detached would collide with a held lock.

        A lock state we could not establish takes the IPC route rather than the
        detached one. Both may be wrong, but only one of them can corrupt a
        rollout: an IPC attempt against a thread the app does not hold comes
        back as an error, while a resume onto a held lock is a second writer.

        `surface` applies to the detached route only. A turn that goes over IPC
        is already in a window the app is rendering, so there is nothing to
        bring forward; a detached one is invisible until it exits, and is
        surfaced then unless this says otherwise.
        """
        resolved = self.resolve(ref, instance)
        self._refuse_if_held_elsewhere(resolved)
        if resolved.info.app_owned or not resolved.info.resumable:
            # The IPC route starts a turn and carries no settings with it, so
            # there is nowhere for these to go. Dropping them silently would
            # run the turn at whatever the thread was already on while the
            # caller believed it had asked for something else -- absence
            # reported as fact, in the shape this codebase keeps finding.
            # Applying them instead would mean mutating the thread's persistent
            # settings as a side effect of sending a message, which is
            # edit_thread's job and not this one's.
            named = [
                name
                for name, value in (
                    ("model", model),
                    ("effort", effort),
                    ("service_tier", service_tier),
                )
                if value is not None
            ]
            if named:
                raise ActionError(
                    f"{resolved.thread_id} routes over IPC (Codex Desktop has it open), and "
                    f"that route cannot carry per-turn settings, so {', '.join(named)} would "
                    "have been ignored rather than applied. Set them on the thread first with "
                    "edit_thread(action='update_settings'), which lands on the next turn, then "
                    "send the message."
                )
            result = self._follower_request(
                resolved,
                "thread-follower-start-turn",
                payloads.start_turn(resolved.thread_id, text),
            )
            return {
                "route": "desktop",
                "instance": resolved.instance.slug,
                "thread": resolved.thread_id,
                "name": resolved.name,
                "result": result,
            }

        run = self.runner(resolved.instance).run(
            resolved.thread_id,
            text,
            sandbox=sandbox or "workspace-write",
            approval=approval or "never",
            model=model,
            effort=effort,
            service_tier=service_tier,
        )
        run.surface = surface
        # Registered for the same reason start_thread's is: while this runs, it
        # and not the app holds the writer lock, and every other verb needs to
        # know that.
        self._register_run(run)
        out = run.as_dict()
        out["name"] = resolved.name
        return out

    def _refuse_a_turn_the_app_cannot_steer(self, resolved: ResolvedThread) -> None:
        """Refuse a steer the app is going to fail slowly and say nothing about.

        The app steers the turn its `lv()` calls the latest one and needs that
        turn to carry a server-assigned id. When it has none the app waits 30s
        for one and then fails -- but the main process abandons a forwarded
        follower request after 5s, so the only thing that reaches us is
        `thread-follower-steer-turn-timeout`. The useful diagnosis never leaves
        the app, which is why this refuses in front of it rather than passing
        the request on and reporting whatever comes back.

        Only age separates a stuck turn from a healthy one: the app appends
        every turn optimistically and fills the id in when the first stream
        event arrives, so a young one without an id is ordinary. Anything that
        cannot be known -- unreadable state, an undated turn -- steers as
        before, because not knowing is not evidence.
        """
        state = self.thread_state(resolved, wait=STALLED_TURN_PROBE_SECONDS)
        if state is None:
            return
        latest = state.latest_turn
        if latest is None or not latest.placeholder:
            return
        age = latest.age_seconds(time.time())
        if age is None or age < STALLED_TURN_SECONDS:
            return
        raise StalledTurnError(
            f"{resolved.thread_id} has had a turn in progress for {age:.0f}s without a turn "
            "id, so Codex Desktop will refuse to steer it -- the app assigns the id from the "
            "turn's first stream event, and for this one that never arrived. Steering it "
            "would spend 5s and come back as `thread-follower-steer-turn-timeout`. Use "
            "send_message instead: the new turn lands after the stuck one and becomes the "
            "turn the app steers."
        )

    def steer_turn(self, ref: str, text: str, instance: str | None = None) -> dict[str, Any]:
        resolved = self.resolve(ref, instance)
        # Before reading any state: a lock held by another writer means neither
        # route reaches the thread at all, which outranks what the app's stream
        # last showed about it. `_follower_request` checks again below, which is
        # deliberate -- the check is a pure read of two cheap fields, and a lock
        # that changed hands during the state read is news worth having.
        self._refuse_if_held_elsewhere(resolved)
        self._refuse_a_turn_the_app_cannot_steer(resolved)
        result = self._follower_request(
            resolved,
            "thread-follower-steer-turn",
            payloads.steer_turn(resolved.thread_id, text, cwd=resolved.info.cwd),
        )
        return {
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "name": resolved.name,
            "result": result,
        }

    def stop_turn(
        self, ref: str, expected_turn_id: str | None = None, instance: str | None = None
    ) -> dict[str, Any]:
        """Interrupt the running turn.

        Read `stopped`, not `ok`. The app answers `{"ok": true}` even when it
        stopped nothing -- both when the thread was already idle and when an
        `expected_turn_id` precondition did not match the running turn. Verified
        live: a stale expected_turn_id returns `ok: true`,
        `interruptedTurnId: null`, and leaves the turn running. The turn id is
        the only real signal.
        """
        resolved = self.resolve(ref, instance)
        own = self.live_run(resolved.thread_id)
        if own is not None:
            # The only way to stop a detached run: it is not in the app, so
            # there is no turn to interrupt over IPC. Without this, a thread
            # start_thread launched -- autonomous, and by default unattended --
            # could not be stopped from here at all.
            return self._stop_detached(resolved, own)
        # A detached writer this process did not spawn looks identical from
        # here, but the pid is not ours: _stop_detached signals a whole process
        # group, and that group belongs to somebody else's agent. Refuse rather
        # than guess, and say whose it is so it can be found.
        holder = resolved.info.holder
        if holder is not None and holder.is_app is False:
            raise ForeignWriterError(
                f"{holder} holds the writer lock on {resolved.thread_id} and is a writer "
                "this process did not start, so there is no turn to interrupt over IPC and "
                "its process group is not ours to signal. Stop it where it was started, or "
                "wait for it to exit."
            )
        params = payloads.interrupt_turn(resolved.thread_id, expected_turn_id=expected_turn_id)
        result = self._follower_request(resolved, "thread-follower-interrupt-turn", params)
        interrupted = result.get("interruptedTurnId")
        return {
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "name": resolved.name,
            "stopped": isinstance(interrupted, str),
            "expected_turn_id": expected_turn_id,
            "interrupted_turn_id": interrupted,
            "goal_pause_error": result.get("goalPauseError"),
        }

    def _stop_detached(self, resolved: ResolvedThread, run: DetachedRun) -> dict[str, Any]:
        """Terminate one of our own detached runs, and its whole process group.

        The group, not just the pid: the run is spawned with
        `start_new_session=True`, so the agent's own children (a build, a test
        run) are in that group and would otherwise outlive it holding the lock.
        """
        stopped = True
        run.stopped = True
        try:
            os.killpg(run.pid, signal.SIGTERM)
        except ProcessLookupError:
            stopped = False  # already gone; the pump will report it
        except OSError as exc:
            raise ActionError(f"could not stop pid {run.pid}: {exc}") from exc
        else:
            try:
                run.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(run.pid, signal.SIGKILL)
        return {
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "name": resolved.name,
            "route": "detached",
            "stopped": stopped,
            "pid": run.pid,
            "returncode": run.returncode,
            "log_path": str(run.log_path),
        }

    def respond(
        self,
        ref: str,
        request_id: int | str,
        kind: str,
        decision: Any = None,
        response: Any = None,
        instance: str | None = None,
        available_decisions: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Answer one pending request. Sends exactly once; never retries.

        A timeout here means the outcome is unknown, not that nothing happened --
        the decision may already have landed, and re-sending could answer a
        different request that has since taken the same slot.
        """
        resolved = self.resolve(ref, instance)
        method, params = payloads.respond(
            resolved.thread_id,
            request_id,
            kind,
            decision=decision,
            response=response,
            available_decisions=available_decisions,
        )
        result = self._follower_request(resolved, method, params)
        return {
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "request_id": request_id,
            "kind": kind,
            "result": result,
        }

    def update_settings(
        self, ref: str, settings: dict[str, Any], instance: str | None = None
    ) -> dict[str, Any]:
        """Change model, reasoning effort, plan mode, service tier, sandbox, etc.

        Takes effect on the next turn, not the running one.
        """
        resolved = self.resolve(ref, instance)
        result = self._follower_request(
            resolved,
            "thread-follower-update-thread-settings",
            payloads.update_thread_settings(resolved.thread_id, settings),
        )
        return {
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "applied": settings,
            "result": result,
        }

    def compact(self, ref: str, instance: str | None = None) -> dict[str, Any]:
        resolved = self.resolve(ref, instance)
        result = self._follower_request(
            resolved, "thread-follower-compact-thread", payloads.compact_thread(resolved.thread_id)
        )
        return {"instance": resolved.instance.slug, "thread": resolved.thread_id, "result": result}

    # -- reading ------------------------------------------------------------

    def thread_state(self, resolved: ResolvedThread, wait: float = SNAPSHOT_WAIT_SECONDS) -> Any:
        """Current projected state for a thread, however it can be had.

        A followed thread already has state kept current by its stream, and the
        app will not resend a snapshot just because we asked -- so read what the
        follow holds rather than waiting for a frame that is not coming.
        """
        manager = self.follow_manager(resolved.instance)
        if manager.is_following(resolved.thread_id):
            held = manager.state_of(resolved.thread_id)
            if held is not None:
                return held
        from . import snapshot as projection

        return projection.project(self.snapshot(resolved, wait=wait))

    def snapshot(self, resolved: ResolvedThread, wait: float = SNAPSHOT_WAIT_SECONDS) -> Any:
        """Take one stream-state frame for a thread.

        Stream state only reaches registered followers, so reading it means
        subscribing. When a persistent follow is already running we must not
        subscribe and unsubscribe around it: the unsubscribe would deregister
        the live follow while our bookkeeping still called it active, leaving a
        follow that silently receives nothing.

        Registered is not the same as healthy, though, and the two were treated
        as one. A follow whose connection dropped is still registered while
        receiving nothing, so skipping the subscribe meant waiting the full
        timeout for a frame that could not arrive and then reporting the thread
        unreadable. So the subscribe is driven by whether state is actually
        arriving, and the *unsubscribe* by whether the follow is ours to keep.
        """
        manager = self.follow_manager(resolved.instance)
        client = self.client(resolved.instance)
        registered = manager.is_following(resolved.thread_id)
        healthy = registered and manager.state_of(resolved.thread_id) is not None

        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4)
        with self._guard:
            self._snapshot_waiters.setdefault(resolved.thread_id, []).append(waiter)
        try:
            if not healthy:
                client.broadcast(FOLLOWING_CHANGED, payloads.follow(resolved.thread_id, True))
            try:
                return waiter.get(timeout=wait)
            except queue.Empty:
                return None
        finally:
            with self._guard:
                waiters = self._snapshot_waiters.get(resolved.thread_id, [])
                if waiter in waiters:
                    waiters.remove(waiter)
                if not waiters:
                    self._snapshot_waiters.pop(resolved.thread_id, None)
            if not registered:
                client.broadcast(FOLLOWING_CHANGED, payloads.follow(resolved.thread_id, False))

    def thread_row(self, inst: Instance, info: ThreadInfo) -> dict[str, Any]:
        """One thread as the MCP surface reports it.

        `rollout` is included because it is the only way to read what a thread
        actually said: a finished turn on an app-owned thread has no result
        anywhere else, and without it callers fall back to shelling out.
        """
        row: dict[str, Any] = {
            "instance": inst.slug,
            "thread": info.thread_id,
            "name": info.name,
            "cwd": info.cwd,
            "route": self.route_for(info),
            # Who is writing, when anyone is. `started_here` below says whether
            # that is one of ours; without the holder there is no way for a
            # caller to tell a foreign writer from an app-held thread at all.
            "holder": str(info.holder) if info.holder is not None else None,
            "lock_known": info.lock_known,
            "age_seconds": info.age_seconds,
            "turn_id": info.turn_id,
            "rollout": str(info.rollout) if info.rollout is not None else None,
        }
        row.update(self.own_run_fields(info.thread_id))
        return row

    def own_run_fields(self, thread_id: str) -> dict[str, Any]:
        """How a run of ours is described, in one place.

        thread_status and list_threads both need it; two hand-rolled copies had
        already drifted apart.
        """
        with self._guard:
            run = self._runs.get(thread_id)
        if run is None:
            return {}
        return {
            "started_here": True,
            "running": run.running,
            "pid": run.pid,
            "log_path": str(run.log_path),
            "returncode": run.returncode,
        }

    def read_thread(
        self,
        ref: str,
        limit: int = 40,
        include_reasoning: bool = False,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """What a thread actually said, read from its rollout.

        Works for every thread, including ones the app is not mounted on and so
        has no live state for.
        """
        from . import transcript

        resolved = self.resolve(ref, instance)
        if resolved.info.rollout is None:
            raise UnknownThreadError(
                f"no rollout on disk for {resolved.thread_id}; nothing to read"
            )
        out = transcript.read(
            resolved.info.rollout, limit=limit, include_reasoning=include_reasoning
        )
        out["instance"] = resolved.instance.slug
        out["thread"] = resolved.thread_id
        out["name"] = resolved.name
        out["route"] = self.route_for(resolved.info)
        return out

    def list_threads(self, instance: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for inst in self._instances:
            if instance is not None and inst.slug != instance:
                continue
            store = self.store(inst)
            held = store.lock_census()
            seen: set[str] = set()
            # Same one lsof sweep feeds both loops, and the same newest-activity
            # ordering list_open() used.
            lock_held = sorted(
                store.describe_many(held.holders),
                key=lambda i: (i.age_seconds is None, i.age_seconds or 0.0),
            )
            for info in lock_held:
                seen.add(info.thread_id)
                out.append(self.thread_row(inst, info))
            # Threads we started hold no lock once idle, so nothing else would
            # list them -- and an orchestrator needs to see the work it launched.
            with self._guard:
                ours = [tid for tid, run in self._runs.items() if run.instance == inst.slug]
            for thread_id in ours:
                if thread_id in seen:
                    continue
                out.append(self.thread_row(inst, store.describe(thread_id, census=held)))
        return out
