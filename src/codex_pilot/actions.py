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

import subprocess
from dataclasses import dataclass
from typing import Any

from . import payloads
from .instances import Instance, discover_instances
from .ipc import IpcClient, IpcUnavailable, RouterError
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

    @property
    def instances(self) -> list[Instance]:
        return self._instances

    def store(self, instance: Instance) -> ThreadStore:
        if instance.slug not in self._stores:
            self._stores[instance.slug] = ThreadStore(instance.codex_home)
        return self._stores[instance.slug]

    def runner(self, instance: Instance) -> DetachedRunner:
        if instance.slug not in self._runners:
            self._runners[instance.slug] = DetachedRunner(instance, self.store(instance))
        return self._runners[instance.slug]

    def client(self, instance: Instance) -> IpcClient:
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
        self._clients[instance.slug] = client
        return client

    def close(self) -> None:
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

    def snapshot(self, resolved: ResolvedThread, wait: float = SNAPSHOT_WAIT_SECONDS) -> Any:
        """Transient follow: subscribe, take one stream-state frame, unsubscribe.

        Stream state is broadcast only to registered followers, so there is no
        way to read it without briefly becoming one.
        """
        client = self.client(resolved.instance)
        client.broadcast(FOLLOWING_CHANGED, payloads.follow(resolved.thread_id, True))
        try:
            deadline_frames: list[Any] = []
            while True:
                frame = client.next_broadcast(timeout=wait)
                if frame is None:
                    return None
                if frame.get("method") != STREAM_STATE_CHANGED:
                    continue
                params = frame.get("params") or {}
                if params.get("conversationId") != resolved.thread_id:
                    continue
                deadline_frames.append(frame)
                return frame
        finally:
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
