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

import contextlib
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import payloads
from .follow import FollowManager, SeqCounter
from .instances import Instance, discover_instances
from .ipc import IpcClient, IpcError, IpcUnavailable, RouterError
from .resume import DetachedRunner
from .threads import AmbiguousThreadError, ThreadInfo, ThreadStore, UnknownThreadError

OWNER_DISCOVERY = "thread-owner-discovery"
FOLLOWING_CHANGED = "thread-stream-following-changed"
STREAM_STATE_CHANGED = "thread-stream-state-changed"
SNAPSHOT_WAIT_SECONDS = 3.0


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
        self._follow: dict[str, FollowManager] = {}
        self._pumps: dict[str, threading.Thread] = {}
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
        existing = self._pumps.get(instance.slug)
        if existing is not None and existing.is_alive():
            return

        def pump() -> None:
            """Keep the connection alive and re-request snapshots after a gap.

            Frames reach the manager through the client's broadcast listener, so
            this loop exists for the two things a listener cannot do: notice the
            connection died, and re-subscribe a thread whose stream desynced. A
            gap is otherwise unrecoverable -- nothing would ever ask for a fresh
            snapshot and the follow would sit silent forever.
            """
            manager = self.follow_manager(instance)
            while not self._stop.is_set():
                try:
                    client = self.client(instance)
                except IpcError:
                    for thread_id in manager.followed:
                        manager.lost(thread_id, "Codex Desktop is not reachable")
                    self._stop.wait(5.0)
                    continue
                try:
                    for thread_id in manager.take_resync_requests():
                        client.broadcast(FOLLOWING_CHANGED, payloads.follow(thread_id, True))
                        client.broadcast(
                            "thread-stream-following-status-requested",
                            {"conversationId": thread_id, "hostId": "local"},
                        )
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

    def client(self, instance: Instance) -> IpcClient:
        # Locked check-then-create: the follow pump and an MCP call can both
        # notice a dropped connection at once, and two clients would mean two
        # handshakes with one of them orphaned and unread.
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
            client.initialize()
            client.add_broadcast_listener(self._make_listener(instance))
            self._clients[instance.slug] = client
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

    def focus_thread(self, ref: str, instance: str | None = None) -> dict[str, Any]:
        """Bring a thread forward in the app so a window claims it.

        The app answers owner discovery only for a thread it is rendering, so a
        thread it holds open in the background is undriveable until something
        surfaces it. The `codex://threads/<id>` deep link is how the app itself
        navigates, and it takes effect in a couple of seconds.
        """
        resolved = self.resolve(ref, instance)
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

    def _follower_request(
        self, resolved: ResolvedThread, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        owner = self.owner_of(resolved)
        client = self.client(resolved.instance)
        response = client.request(method, params, target_client_id=owner)
        result = response.get("result")
        return result if isinstance(result, dict) else {}

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

    def list_threads(self, instance: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for inst in self._instances:
            if instance is not None and inst.slug != instance:
                continue
            for info in self.store(inst).list_open():
                out.append(
                    {
                        "instance": inst.slug,
                        "thread": info.thread_id,
                        "name": info.name,
                        "cwd": info.cwd,
                        "route": "desktop" if info.app_owned else "detached",
                        "age_seconds": info.age_seconds,
                        "turn_id": info.turn_id,
                    }
                )
        return out
