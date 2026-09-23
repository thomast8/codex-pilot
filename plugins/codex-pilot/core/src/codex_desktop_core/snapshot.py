"""Projection of a thread's stream state into the few facts we act on.

The app broadcasts a large `conversationState` (~118KB for a short thread). We
keep three things from it: whether a turn is running, which turn, and what the
thread is currently asking for. Everything else is the app's business.

Pending requests are the interesting part. Each one carries an
`availableDecisions` list -- the app saying which answers *this* request accepts.
That list is not the same for every request: a blocked-network command offers
`accept`, `acceptWithExecpolicyAmendment` and `cancel`, but no `decline`.
Answering with a decision the app did not offer is a silent no-op at best, so
the live list is the authority and the constants in `payloads` are only a
vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .payloads import REQUEST_METHOD_TO_KIND

RUNTIME_ACTIVE = "active"
RUNTIME_IDLE = "idle"
STATUS_IN_PROGRESS = "inProgress"


@dataclass(frozen=True)
class PendingRequest:
    """Something the thread is waiting on an answer for."""

    request_id: int | str
    method: str
    kind: str | None
    summary: str
    reason: str | None
    cwd: str | None
    available_decisions: list[Any] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def answerable(self) -> bool:
        """Whether we know which follower method answers this kind."""
        return self.kind is not None

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. One definition, because two would drift."""
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "summary": self.summary,
            "reason": self.reason,
            "cwd": self.cwd,
            "available_decisions": self.available_decisions,
            "answerable": self.answerable,
        }


@dataclass(frozen=True)
class LatestTurn:
    """The turn the app's steer gate will act on.

    A separate fact from `ThreadState.turn_id`, and deliberately so: that one
    answers "is a turn running", scanning for any in-progress turn carrying an
    id, which is what `stop_turn` wants. This one answers "which turn will the
    app steer".

    They agree on the ordinary thread. They part company once a thread carries
    two turns in progress at once, and then only in one direction: a turn with
    no id sitting *behind* one that has an id reads as that real id in
    `turn_id` and as the id-less turn here, because the app steers the newest
    turn and not the healthiest one. The same pair in the other order agrees,
    which is why the live capture this was built from showed no disagreement.
    """

    status: str | None
    turn_id: str | None
    started_at_ms: int | None

    @property
    def placeholder(self) -> bool:
        """In progress, with no server-assigned id.

        The app appends a turn optimistically and fills the id in when the
        first stream event arrives, so this is the ordinary state of a turn
        that has only just started. It is also the state of one the app gave up
        on -- see `age_seconds`, because nothing else tells the two apart.
        """
        return self.status == STATUS_IN_PROGRESS and not self.turn_id

    def as_dict(self, now: float) -> dict[str, Any]:
        """The wire shape. One definition, because two would drift."""
        return {
            "status": self.status,
            "turn_id": self.turn_id,
            "age_seconds": self.age_seconds(now),
            "placeholder": self.placeholder,
        }

    def age_seconds(self, now: float) -> float | None:
        """How long the turn has been running, given `now` as a unix timestamp.

        None when the app did not stamp a start time: age is what separates a
        turn that just started from one that is never going to finish, so an
        unknown age has to stay unknown rather than default to zero.
        """
        if self.started_at_ms is None:
            return None
        return max(0.0, now - self.started_at_ms / 1000.0)


@dataclass(frozen=True)
class ThreadState:
    runtime: str | None
    turn_id: str | None
    latest_turn: LatestTurn | None
    pending: list[PendingRequest]
    revision: int | None
    cwd: str | None
    model: str | None
    effort: str | None
    collaboration_mode: str | None
    service_tier: str | None
    approvals_reviewer: str | None
    goal: dict[str, Any] | None

    @property
    def busy(self) -> bool:
        return self.runtime == RUNTIME_ACTIVE

    @property
    def idle(self) -> bool:
        return self.runtime == RUNTIME_IDLE


def _summarise(method: str, params: dict[str, Any]) -> str:
    """One line a human (or Claude) can judge the request from."""
    if method == "item/commandExecution/requestApproval":
        return str(params.get("command") or "(no command)")
    if method == "item/fileChange/requestApproval":
        changes = params.get("changes") or params.get("fileChanges") or []
        if isinstance(changes, list) and changes:
            paths = [str(c.get("path") or c) for c in changes if c]
            return "file changes: " + ", ".join(paths[:5])
        return "file changes"
    if method == "item/permissions/requestApproval":
        return f"permissions: {params.get('permissions') or params.get('reason') or ''}".strip()
    if method == "item/tool/requestUserInput":
        return str(params.get("prompt") or params.get("question") or "input requested")
    if method == "mcpServer/elicitation/request":
        return str(params.get("message") or "MCP elicitation")
    return method


def parse_pending(requests: list[Any]) -> list[PendingRequest]:
    out: list[PendingRequest] = []
    for raw in requests or []:
        if not isinstance(raw, dict):
            continue
        method = str(raw.get("method") or "")
        maybe_params = raw.get("params")
        params: dict[str, Any] = maybe_params if isinstance(maybe_params, dict) else {}
        request_id = raw.get("id")
        if request_id is None:
            continue
        out.append(
            PendingRequest(
                # The app assigns integer ids; keep the native type rather than
                # stringifying, because it is echoed straight back on answer.
                request_id=request_id,
                method=method,
                kind=REQUEST_METHOD_TO_KIND.get(method),
                summary=_summarise(method, params),
                reason=params.get("reason"),
                cwd=params.get("cwd"),
                available_decisions=list(params.get("availableDecisions") or []),
                params=params,
            )
        )
    return out


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _turn_id_from_pending(pending: list[PendingRequest]) -> str | None:
    for req in pending:
        raw = req.params.get("turnId")
        if isinstance(raw, str) and raw:
            return raw
    return None


def _active_turn_id(state: dict[str, Any]) -> str | None:
    """The id of the turn currently in progress, if the app has assigned one.

    Turn history is keyed two ways: `turn:<turnId>` once the server has
    confirmed a turn, and `tail:<n>:local:<uuid>` for one the window created
    optimistically and has no id for yet. An in-progress turn can legitimately
    have no id, so a null here means "not yet assigned", not "no turn".
    """
    history = _as_dict(_as_dict(state.get("turnHistory")).get("history"))
    entities = history.get("entitiesByKey")
    if not isinstance(entities, dict):
        return None
    for key, entity in entities.items():
        if not isinstance(entity, dict) or entity.get("status") != "inProgress":
            continue
        raw = entity.get("turnId")
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(key, str) and key.startswith("turn:"):
            return key.split(":", 1)[1]
    return None


def _turn_entity(entity: Any) -> LatestTurn | None:
    if not isinstance(entity, dict):
        return None
    status = entity.get("status")
    turn_id = entity.get("turnId")
    started = entity.get("turnStartedAtMs")
    return LatestTurn(
        status=status if isinstance(status, str) else None,
        turn_id=turn_id if isinstance(turn_id, str) and turn_id else None,
        started_at_ms=int(started) if isinstance(started, (int, float)) else None,
    )


def _entry_turn(entities: dict[str, Any], entry: Any) -> LatestTurn | None:
    """Resolve one island entry through the entity table, as the app does."""
    if not isinstance(entry, dict):
        return None
    key = entry.get("value")
    return _turn_entity(entities.get(key)) if isinstance(key, str) else None


def _latest_turn(state: dict[str, Any]) -> LatestTurn | None:
    """The turn the app would steer -- its `lv()`, modelled from the bundle.

    Canonical history is a list of islands. The app takes the tail island when
    that island is closed at its newer boundary (`av`), and otherwise flattens
    every island and takes the last entry. Those pick the same turn whenever
    the tail island has entries; they differ only for an empty tail, where the
    closed branch stops at nothing and the flattening one keeps walking back.
    A history that is present but malformed reads as unknown, not as empty.
    """
    holder = _as_dict(state.get("turnHistory"))
    if holder.get("kind") != "canonical":
        turns = state.get("turns")
        return _turn_entity(turns[-1]) if isinstance(turns, list) and turns else None

    history = _as_dict(holder.get("history"))
    islands = history.get("islands")
    entities = _as_dict(history.get("entitiesByKey"))
    if not isinstance(islands, list):
        return None

    tail = islands[-1] if islands else None
    if isinstance(tail, dict):
        entries = tail.get("entries")
        closed = _as_dict(tail.get("newerBoundary")).get("status") == "exhausted"
        if closed:
            last = entries[-1] if isinstance(entries, list) and entries else None
            return _entry_turn(entities, last)

    for maybe in reversed(islands):
        entries = maybe.get("entries") if isinstance(maybe, dict) else None
        if isinstance(entries, list) and entries:
            return _entry_turn(entities, entries[-1])
    return None


def project(frame: dict[str, Any] | None) -> ThreadState | None:
    """Turn a `thread-stream-state-changed` frame into a ThreadState.

    Returns None when there is nothing to read -- callers must treat that as
    "could not read", never as "nothing pending", or auto-answering would
    silently conclude a thread is waiting on nothing.
    """
    if not frame:
        return None
    change = (frame.get("params") or {}).get("change") or {}
    state = change.get("conversationState")
    if not isinstance(state, dict):
        return None

    # Every sub-object is checked rather than assumed: a patch can replace any
    # key with any value, and an AttributeError here would kill the reader.
    settings = _as_dict(state.get("latestThreadSettings"))
    permissions = _as_dict(state.get("currentPermissions"))
    runtime = _as_dict(state.get("threadRuntimeStatus")).get("type")
    pending = parse_pending(state.get("requests") or [])

    turn_id = _active_turn_id(state) or _turn_id_from_pending(pending)

    return ThreadState(
        runtime=runtime if isinstance(runtime, str) else None,
        turn_id=turn_id,
        latest_turn=_latest_turn(state),
        pending=pending,
        revision=change.get("revision"),
        cwd=state.get("cwd"),
        model=settings.get("model"),
        effort=settings.get("effort"),
        collaboration_mode=_as_dict(settings.get("collaborationMode")).get("mode"),
        service_tier=settings.get("serviceTier"),
        approvals_reviewer=permissions.get("approvalsReviewer"),
        goal=_as_dict(state.get("threadGoal"))
        or _as_dict(state.get("completedThreadGoal"))
        or None,
    )


class PatchError(Exception):
    """A patch could not be applied to the state we hold."""


def apply_patches(state: dict[str, Any], patches: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the app's patch ops to a conversationState.

    Ops are `add`, `replace` and `remove`, and `path` is a list of keys rather
    than a JSON-Pointer string. Mutates a copy, so a failed patch leaves the
    caller's state untouched -- half-applied state is worse than stale state,
    because it looks current.
    """
    import copy

    updated = copy.deepcopy(state)
    for patch in patches:
        path = patch.get("path")
        op = patch.get("op")
        if not isinstance(path, list) or not path:
            raise PatchError(f"patch has no usable path: {patch!r}")
        target: Any = updated
        for index, key in enumerate(path[:-1]):
            if isinstance(target, dict):
                if key not in target:
                    if op != "add":
                        # Only `add` may create a path. Fabricating one for a
                        # replace or remove invents keys the app never sent, and
                        # our state diverges from the app's with nothing to
                        # notice it.
                        raise PatchError(f"{op} at {path}: no key {key!r} at depth {index}")
                    target[key] = {}
                target = target[key]
            elif isinstance(target, list) and isinstance(key, int):
                try:
                    target = target[key]
                except IndexError as exc:
                    raise PatchError(f"{op} at {path}: index {key} out of range") from exc
            else:
                raise PatchError(f"cannot walk {path} at {key!r}")
        leaf = path[-1]
        try:
            if op in ("add", "replace"):
                if isinstance(target, list) and isinstance(leaf, int):
                    if op == "add":
                        target.insert(leaf, patch.get("value"))
                    else:
                        target[leaf] = patch.get("value")
                else:
                    target[leaf] = patch.get("value")
            elif op == "remove":
                if isinstance(target, dict):
                    target.pop(leaf, None)
                elif isinstance(target, list) and isinstance(leaf, int):
                    del target[leaf]
                else:
                    # Silently succeeding here would let our state drift from
                    # the app's with no resync to correct it.
                    raise PatchError(
                        f"remove at {path}: cannot address {leaf!r} on {type(target).__name__}"
                    )
            else:
                raise PatchError(f"unknown patch op {op!r}")
        except (KeyError, IndexError, TypeError) as exc:
            raise PatchError(f"could not apply {op} at {path}: {exc}") from exc
    return updated


def state_from_frame(frame: dict[str, Any]) -> tuple[str | None, int | None, int | None, Any]:
    """(change type, baseRevision, revision, payload) for a stream-state frame."""
    change = (frame.get("params") or {}).get("change") or {}
    return (
        change.get("type"),
        change.get("baseRevision"),
        change.get("revision"),
        change.get("conversationState")
        if change.get("type") == "snapshot"
        else change.get("patches"),
    )
