"""Builders for the `thread-follower-*` request params.

Each shape is pinned from the installed app bundle's renderer (which destructures
the params) cross-checked against the app-server JSON schema (which types the
values). Where remodex disagrees, the bundle wins -- it targets a different app
build. See docs/protocol.md.

These are plain functions returning plain dicts so the literal JSON is testable
without a socket.
"""

from __future__ import annotations

import uuid
from typing import Any

# Renderer: `interruptConversation(conversationId, mode, expectedTurnId)`.
# "user-stop" is what the app's own Stop button sends; "descendant-cleanup" is
# used when tearing down subagents.
INTERRUPT_MODE_USER_STOP = "user-stop"
INTERRUPT_MODE_DESCENDANT_CLEANUP = "descendant-cleanup"

# Which follower method answers which pending request. The `request.method`
# values come from the bundle's bulk-decline routine, which enumerates every
# kind a thread can ask.
RESPOND_METHODS: dict[str, str] = {
    "command": "thread-follower-command-approval-decision",
    "file": "thread-follower-file-approval-decision",
    "permissions": "thread-follower-permissions-request-approval-response",
    "user_input": "thread-follower-submit-user-input",
    "elicitation": "thread-follower-submit-mcp-server-elicitation-response",
}

# The approval kinds carry `decision`; the input kinds carry `response`.
DECISION_KINDS = frozenset({"command", "file"})

REQUEST_METHOD_TO_KIND: dict[str, str] = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file",
    "item/permissions/requestApproval": "permissions",
    "item/tool/requestUserInput": "user_input",
    "mcpServer/elicitation/request": "elicitation",
}

# Single-shot answers. The remaining values Codex accepts -- acceptForSession,
# acceptWithExecpolicyAmendment, applyNetworkPolicyAmendment -- are persistent
# grants that outlive the turn, so they are never a default here; a caller that
# wants one passes it explicitly.
ACCEPT = "accept"
DECLINE = "decline"
CANCEL = "cancel"  # denies *and* interrupts the turn
PERSISTENT_GRANTS = frozenset({"acceptForSession"})


def _decision_label(decision: Any) -> str:
    """Name a decision that may be a bare string or a single-key object."""
    if isinstance(decision, str):
        return decision
    if isinstance(decision, dict) and len(decision) == 1:
        return str(next(iter(decision)))
    return repr(decision)


def _decision_offered(decision: Any, available: list[Any]) -> bool:
    return _decision_label(decision) in {_decision_label(d) for d in available}


def text_input(text: str) -> list[dict[str, Any]]:
    """`input` is an array of UserInput; this is the text variant.

    Confirmed twice: the app-server schema types TurnStartParams.input and
    TurnSteerParams.input as arrays, and remodex guards with Array.isArray.
    """
    return [{"type": "text", "text": text, "text_elements": []}]


def start_turn(
    conversation_id: str,
    text: str,
    cwd: str | None = None,
    model: str | None = None,
    client_user_message_id: str | None = None,
) -> dict[str, Any]:
    """`thread-follower-start-turn` params (v2).

    The renderer wraps a turn/start request as `turnStart: {request, context}`
    and throws `Turn request thread does not match the conversation` unless
    `request.threadId` equals the conversationId -- so threadId is not optional
    even though it looks redundant beside conversationId.
    """
    request: dict[str, Any] = {
        "threadId": conversation_id,
        "input": text_input(text),
        "clientUserMessageId": client_user_message_id or str(uuid.uuid4()),
    }
    if cwd is not None:
        request["cwd"] = cwd
    if model is not None:
        request["model"] = model
    return {
        "conversationId": conversation_id,
        "turnStart": {
            "request": request,
            "context": {
                "attachments": [],
                "commentAttachments": [],
                "inheritThreadSettings": True,
            },
        },
    }


def steer_turn(
    conversation_id: str,
    text: str,
    cwd: str | None = None,
    workspace_roots: list[str] | None = None,
    client_user_message_id: str | None = None,
) -> dict[str, Any]:
    """`thread-follower-steer-turn` params (v1).

    The app-server's TurnSteerParams requires `expectedTurnId`, but the IPC
    handler does not take one: the owning window derives it from its own active
    turn. Passing one here would be ignored.

    `restoreMessage` is not optional despite looking like a detail. The renderer
    reads `restoreMessage.cwd` and `restoreMessage.context.workspaceRoots` off it
    without a guard, so omitting it fails inside the app with
    `Cannot read properties of undefined (reading 'cwd')` rather than a protocol
    error. `cwd` itself may be null (the app falls back to the conversation's
    own cwd); the surrounding object and its `context` may not.
    """
    return {
        "conversationId": conversation_id,
        "input": text_input(text),
        "clientUserMessageId": client_user_message_id or str(uuid.uuid4()),
        "attachments": [],
        "additionalContext": None,
        "serviceTier": None,
        "restoreMessage": {
            "cwd": cwd,
            "context": {
                "workspaceRoots": workspace_roots or ([cwd] if cwd else []),
                "collaborationMode": None,
            },
        },
    }


def interrupt_turn(
    conversation_id: str,
    expected_turn_id: str | None = None,
    mode: str = INTERRUPT_MODE_USER_STOP,
) -> dict[str, Any]:
    """`thread-follower-interrupt-turn` params.

    Omitting `expectedTurnId` drops the request to version 3 (see registry) and
    removes the precondition -- the stop then lands on whatever turn is running,
    which may not be the one you looked at.
    """
    params: dict[str, Any] = {"conversationId": conversation_id, "mode": mode}
    if expected_turn_id is not None:
        params["expectedTurnId"] = expected_turn_id
    return params


def respond(
    conversation_id: str,
    request_id: int | str,
    kind: str,
    decision: Any = None,
    response: Any = None,
    available_decisions: list[Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """(method, params) for answering one pending request.

    Returns the method as well because each kind routes to a different
    `thread-follower-*` method, all v1, all `(conversationId, requestId, ...)`.

    `request_id` keeps whatever type the app gave it -- ids arrive as integers
    and are echoed straight back, so stringifying breaks the match.

    `available_decisions` is the request's own list of acceptable answers. It
    varies per request (a blocked-network command offers accept /
    acceptWithExecpolicyAmendment / cancel but no decline), and an answer the
    app did not offer is ignored rather than refused, so check against it when
    it is known.
    """
    try:
        method = RESPOND_METHODS[kind]
    except KeyError:
        known = ", ".join(sorted(RESPOND_METHODS))
        raise ValueError(f"unknown request kind {kind!r}; expected one of: {known}") from None

    params: dict[str, Any] = {"conversationId": conversation_id, "requestId": request_id}
    if kind in DECISION_KINDS:
        if decision is None:
            raise ValueError(f"{kind!r} requests need a `decision`")
        if available_decisions and not _decision_offered(decision, available_decisions):
            offered = ", ".join(_decision_label(d) for d in available_decisions)
            raise ValueError(
                f"{_decision_label(decision)} is not offered for this request; available: {offered}"
            )
        params["decision"] = decision
    else:
        if response is None:
            raise ValueError(f"{kind!r} requests need a `response`")
        params["response"] = response
    return method, params


# Fields of `latestThreadSettings` that the app itself round-trips. Confirmed
# against a live snapshot; `collaborationMode.mode` is plan mode, and
# `serviceTier` is the fast/priority lever.
THREAD_SETTING_FIELDS = frozenset(
    {
        "cwd",
        "model",
        "effort",
        "summary",
        "personality",
        "serviceTier",
        "collaborationMode",
        "multiAgentMode",
        "approvalPolicy",
        "approvalsReviewer",
        "sandboxPolicy",
        "permissions",
    }
)

# The four SandboxPolicy variants, from the app-server schema. `networkAccess`
# lives here, and it is the switch that decides whether a thread can reach
# the network at all: workspaceWrite defaults it to false.
SANDBOX_POLICY_TYPES = frozenset(
    {"workspaceWrite", "readOnly", "dangerFullAccess", "externalSandbox"}
)

COLLABORATION_MODES = frozenset({"default", "plan"})
SERVICE_TIERS = frozenset({"default", "flex", "priority", "scale"})


def collaboration_mode(
    mode: str, model: str, reasoning_effort: str | None = None, instructions: str | None = None
) -> dict[str, Any]:
    """A `collaborationMode` value -- this is how plan mode is turned on.

    Both halves are required by the app-server: `{mode, settings}` where
    `settings.model` is mandatory. Sending `{"mode": "plan"}` alone is rejected
    with `Invalid request: missing field 'settings'`, which reads like the
    *outer* settings object is missing rather than this nested one.
    """
    if mode not in COLLABORATION_MODES:
        raise ValueError(f"mode must be one of {sorted(COLLABORATION_MODES)}")
    settings: dict[str, Any] = {"model": model}
    if reasoning_effort is not None:
        settings["reasoning_effort"] = reasoning_effort
    if instructions is not None:
        settings["developer_instructions"] = instructions
    return {"mode": mode, "settings": settings}


def update_thread_settings(conversation_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    """`thread-follower-update-thread-settings` params (v1).

    Applies to the *next* turn -- the renderer calls
    `updateThreadSettingsForNextTurn`, so a running turn keeps the settings it
    started with.
    """
    unknown = set(settings) - THREAD_SETTING_FIELDS
    if unknown:
        known = ", ".join(sorted(THREAD_SETTING_FIELDS))
        raise ValueError(f"unknown thread settings {sorted(unknown)}; known fields: {known}")
    collab = settings.get("collaborationMode")
    if collab is not None:
        mode = collab.get("mode")
        if mode not in COLLABORATION_MODES:
            raise ValueError(f"collaborationMode.mode must be one of {sorted(COLLABORATION_MODES)}")
        if not (collab.get("settings") or {}).get("model"):
            raise ValueError(
                "collaborationMode needs settings.model -- build it with "
                "payloads.collaboration_mode(mode, model)"
            )
    tier = settings.get("serviceTier")
    if tier is not None and tier not in SERVICE_TIERS:
        raise ValueError(f"serviceTier must be one of {sorted(SERVICE_TIERS)}")
    _check_sandbox_settings(settings)
    return {"conversationId": conversation_id, "threadSettings": settings}


def _check_sandbox_settings(settings: dict[str, Any]) -> None:
    """Catch the two sandbox mistakes the app answers unhelpfully.

    `permissions` is a *named profile id*, not a permission object: passing the
    obvious `{"network": {"enabled": true}}` fails with "invalid type: map,
    expected a string", which says nothing about what was wanted. And the two
    fields are mutually exclusive per the schema.
    """
    policy = settings.get("sandboxPolicy")
    permissions = settings.get("permissions")

    if permissions is not None and not isinstance(permissions, str):
        raise ValueError(
            "permissions is a named profile id (a string), not a permission object. "
            "To grant network access use sandboxPolicy, e.g. "
            '{"type": "workspaceWrite", "networkAccess": true}'
        )
    if permissions is not None and policy is not None:
        raise ValueError("permissions and sandboxPolicy cannot be combined; pass one")
    if policy is None:
        return
    if not isinstance(policy, dict):
        raise ValueError(
            'sandboxPolicy is an object, e.g. {"type": "workspaceWrite", "networkAccess": true}'
        )
    kind = policy.get("type")
    if kind not in SANDBOX_POLICY_TYPES:
        raise ValueError(
            f"sandboxPolicy.type must be one of {sorted(SANDBOX_POLICY_TYPES)}; "
            f"got {kind!r}. Note these are camelCase, not the CLI's "
            "'workspace-write' spelling."
        )


def compact_thread(conversation_id: str) -> dict[str, Any]:
    """`thread-follower-compact-thread` params (v1)."""
    return {"conversationId": conversation_id}


def follow(conversation_id: str, following: bool = True) -> dict[str, Any]:
    """`thread-stream-following-changed` broadcast params (v1)."""
    return {"conversationId": conversation_id, "hostId": "local", "following": following}


def owner_discovery(conversation_id: str) -> dict[str, Any]:
    """`thread-owner-discovery` params (v1)."""
    return {"hostId": "local", "conversationId": conversation_id}
