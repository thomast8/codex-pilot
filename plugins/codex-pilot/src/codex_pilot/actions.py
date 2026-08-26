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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import payloads
from .follow import EVENT_RUN_FAILED, EVENT_TURN_COMPLETED, FollowManager, SeqCounter
from .instances import Instance, discover_instances
from .ipc import IpcClient, IpcError, IpcUnavailable, RouterError
from .resume import DetachedRun, DetachedRunner, scan_for_thread_id
from .threads import (
    AmbiguousThreadError,
    ThreadError,
    ThreadInfo,
    ThreadStore,
    UnknownThreadError,
)

OWNER_DISCOVERY = "thread-owner-discovery"
FOLLOWING_CHANGED = "thread-stream-following-changed"
STREAM_STATE_CHANGED = "thread-stream-state-changed"
SNAPSHOT_WAIT_SECONDS = 3.0
# Finished runs stay listed so threads we started remain visible; the cap
# keeps a long orchestration session from growing the map without end.
MAX_TRACKED_RUNS = 200

# Route values. `detached_running` is the state that did not exist before
# start_thread: a lock held by one of our own children, so neither route
# works until it exits.
ROUTE_DESKTOP = "desktop"
ROUTE_DETACHED = "detached"
ROUTE_RUNNING = "detached_running"

# An unmounted thread costs the router's full discovery timeout (~10s
# measured) before it answers no-client-found, while a mounted one
# answers in ~0.4s. Probing serially is therefore unusable on a real
# instance; requests are multiplexed by id, so they go concurrently.
CENSUS_WORKERS = 8


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

    def __init__(self, instances: list[Instance] | None = None) -> None:
        self._instances = instances if instances is not None else discover_instances()
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
        self._guard = threading.RLock()
        self._snapshot_waiters: dict[str, list[queue.Queue[dict[str, Any]]]] = {}

    @property
    def instances(self) -> list[Instance]:
        return self._instances

    def store(self, instance: Instance) -> ThreadStore:
        with self._guard:
            if instance.slug not in self._stores:
                self._stores[instance.slug] = ThreadStore(instance.codex_home)
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
                while not self._stop.is_set():
                    # First, and before anything that needs the app: a detached
                    # run finishing is news even when Codex Desktop is not running.
                    self._adopt_late_ids(instance)
                    self._reap_runs(instance)
                    try:
                        # Reconnects are noticed inside client(), which queues
                        # the re-subscribe this loop then broadcasts.
                        client = self.client(instance)
                    except IpcError:
                        for thread_id in manager.followed:
                            manager.lost(thread_id, "Codex Desktop is not reachable")
                        # Detached runs do not need the app, so keep checking on
                        # them promptly even while it is unreachable.
                        self._stop.wait(0.5 if self._has_pending_runs(instance) else 5.0)
                        continue
                    try:
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
                        if client.is_closed:
                            for thread_id in manager.followed:
                                manager.lost(thread_id, "IPC connection closed")
                            self._stop.wait(2.0)
                            continue
                    except IpcError:
                        self._stop.wait(2.0)
                        continue
                    except Exception:  # noqa: BLE001 - a bad frame must not kill the pump
                        self._stop.wait(1.0)
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
        """
        finished: list[tuple[str, DetachedRun]] = []
        with self._guard:
            for thread_id, run in self._runs.items():
                if run.instance != instance.slug or run.reported or run.running:
                    continue
                run.reported = True
                finished.append((thread_id, run))
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
                },
            )

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
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            merged: list[dict[str, Any]] = []
            dropped = 0
            following: list[str] = []
            for manager in managers:
                got = manager.collect(threads, after=after, wait_seconds=0.0)
                merged.extend(got["events"])
                dropped += got["dropped"]
                following.extend(got["following"])
            if merged or time.monotonic() >= deadline:
                merged.sort(key=lambda e: e["seq"])
                return {
                    "events": merged,
                    "cursor": merged[-1]["seq"] if merged else after,
                    "dropped": dropped,
                    "following": sorted(set(following)),
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
                if existing is not None and not existing.is_closed:
                    return existing
                socket_path = instance.socket_path()
            if socket_path is None:
                raise IpcUnavailable(
                    f"Codex Desktop instance {instance.slug!r} is not running "
                    f"(no socket under {instance.codex_home})"
                )
            client = IpcClient(socket_path=socket_path)
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

    def owner_of(self, resolved: ResolvedThread) -> str:
        """The clientId of the window that owns this thread."""
        client = self.client(resolved.instance)
        try:
            response = client.request(OWNER_DISCOVERY, payloads.owner_discovery(resolved.thread_id))
        except RouterError as exc:
            if exc.error == "no-client-found":
                if resolved.info.app_owned:
                    raise UnclaimedThreadError(
                        f"{resolved.thread_id} holds a writer lock ({resolved.info.holder}) but "
                        "no window claims it: the app has the thread open without showing it. "
                        "Bring it forward with focus_thread and retry. It cannot be resumed "
                        "detached either, because the lock is taken. If focusing does not help, "
                        "suspect version drift and run scripts/extract_registry.py --check."
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

    def probe_mounted(self, resolved: ResolvedThread) -> str | None:
        """The owning client id if the app is mounted on this thread, else None."""
        try:
            return self.owner_of(resolved)
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
        mount: bool = True,
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
        """
        before = self.census(threads, instance)
        if not mount or not before["unmounted"]:
            return {**before, "focused": [], "mounted_by_sync": []}

        focused = []
        for thread_id in before["unmounted"]:
            resolved = self.resolve(thread_id)
            if self.live_run(resolved.thread_id) is not None:
                continue
            self.focus_thread(thread_id)
            focused.append(thread_id)
        if focused:
            self._stop.wait(settle_seconds)
        after = self.census(threads, instance)
        gained = sorted(set(after["mounted"]) - set(before["mounted"]))
        return {**after, "focused": focused, "mounted_by_sync": gained}

    def focus_thread(self, ref: str, instance: str | None = None) -> dict[str, Any]:
        """Bring a thread forward in the app so a window claims it.

        The app answers owner discovery only for a thread it is rendering, so a
        thread it holds open in the background is undriveable until something
        surfaces it. The `codex://threads/<id>` deep link is how the app itself
        navigates, and it takes effect in a couple of seconds.
        """
        resolved = self.resolve(ref, instance)
        self._refuse_if_ours(resolved)
        url = f"codex://threads/{resolved.thread_id}"
        subprocess.run(["open", url], check=False, capture_output=True)
        return {
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "name": resolved.name,
            "opened": url,
            "note": "give the app a moment, then retry the call that failed",
        }

    # -- mutating verbs -----------------------------------------------------

    def _refuse_if_ours(self, resolved: ResolvedThread) -> None:
        """Stop before any verb that assumes the app holds the lock.

        `app_owned` only means *someone* holds the writer lock, and since
        start_thread that someone can be one of our own `codex exec` children.
        Without this check `owner_of` reports no-client-found, the caller is
        told the app has the thread open without showing it, and the suggested
        remedy -- focus_thread -- asks Codex Desktop to open a thread our own
        writer is still holding. That is the two-writer case the lock exists to
        prevent, reached by following our own error message.
        """
        own = self.live_run(resolved.thread_id)
        if own is None:
            return
        raise ActionError(
            f"a detached run codex-pilot started (pid {own.pid}) still holds the writer "
            f"lock on {resolved.thread_id}, so it cannot be driven through the app. "
            f"Wait for it (collect_events reports turn_completed when it exits), read "
            f"{own.log_path}, or stop_turn to terminate it. Do not focus it in the app "
            "while it is running -- that would put a second writer on the rollout."
        )

    def _follower_request(
        self, resolved: ResolvedThread, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._refuse_if_ours(resolved)
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

        Three states, not two. A lock holder is not necessarily the app: while
        one of our own detached runs is going it holds the lock too. Calling
        that 'desktop' would send callers to an IPC route that cannot work, and
        calling it 'detached' would promise it is free to resume when it is
        not -- so it gets its own value.
        """
        if self.live_run(info.thread_id) is not None:
            return ROUTE_RUNNING
        return ROUTE_DESKTOP if info.app_owned else ROUTE_DETACHED

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
    ) -> dict[str, Any]:
        """Create a new thread and start its first turn, in a place you name.

        Returns as soon as the thread has an id; the agent keeps working in the
        background. Where it works is never inferred: either `cwd` names an
        existing directory, or `repo` plus `branch` makes a worktree for it,
        laid out the way Codex lays one out. A thread started from wherever the
        caller happened to be is the failure this exists to prevent.
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
        )
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
    ) -> dict[str, Any]:
        """Start a turn, by whichever route the thread's lock state permits.

        A thread the app has open can only be driven over IPC; one nothing holds
        can only be driven detached. The writer lock decides, and it is never
        worked around -- if the app owns the thread but no window claims it,
        that is drift, and running detached would collide with a held lock.
        """
        resolved = self.resolve(ref, instance)
        self._refuse_if_ours(resolved)
        if resolved.info.app_owned:
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
        )
        # Registered for the same reason start_thread's is: while this runs, it
        # and not the app holds the writer lock, and every other verb needs to
        # know that.
        self._register_run(run)
        out = run.as_dict()
        out["name"] = resolved.name
        return out

    def steer_turn(self, ref: str, text: str, instance: str | None = None) -> dict[str, Any]:
        resolved = self.resolve(ref, instance)
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
        """
        manager = self.follow_manager(resolved.instance)
        client = self.client(resolved.instance)
        already_following = manager.is_following(resolved.thread_id)

        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4)
        with self._guard:
            self._snapshot_waiters.setdefault(resolved.thread_id, []).append(waiter)
        try:
            if not already_following:
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
            if not already_following:
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
            held = store.lock_holders()
            seen: set[str] = set()
            # Same one lsof sweep feeds both loops, and the same newest-activity
            # ordering list_open() used.
            lock_held = sorted(
                store.describe_many(held),
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
                out.append(self.thread_row(inst, store.describe(thread_id, holders=held)))
        return out
