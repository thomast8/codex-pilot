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


@dataclass(frozen=True)
class ThreadState:
    runtime: str | None
    turn_id: str | None
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

    settings = state.get("latestThreadSettings") or {}
    permissions = state.get("currentPermissions") or {}
    runtime = (state.get("threadRuntimeStatus") or {}).get("type")
    pending = parse_pending(state.get("requests") or [])

    turn_id = None
    for req in pending:
        if req.params.get("turnId"):
            turn_id = str(req.params["turnId"])
            break

    return ThreadState(
        runtime=runtime,
        turn_id=turn_id,
        pending=pending,
        revision=change.get("revision"),
        cwd=state.get("cwd"),
        model=settings.get("model"),
        effort=settings.get("effort"),
        collaboration_mode=(settings.get("collaborationMode") or {}).get("mode"),
        service_tier=settings.get("serviceTier"),
        approvals_reviewer=permissions.get("approvalsReviewer"),
        goal=state.get("threadGoal") or state.get("completedThreadGoal"),
    )
