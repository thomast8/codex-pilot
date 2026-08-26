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
    request_id: str,
    kind: str,
    decision: Any = None,
    response: Any = None,
) -> tuple[str, dict[str, Any]]:
    """(method, params) for answering one pending request.

    Returns the method as well because each kind routes to a different
    `thread-follower-*` method, all v1, all `(conversationId, requestId, ...)`.
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
        params["decision"] = decision
    else:
        if response is None:
            raise ValueError(f"{kind!r} requests need a `response`")
        params["response"] = response
    return method, params


def follow(conversation_id: str, following: bool = True) -> dict[str, Any]:
    """`thread-stream-following-changed` broadcast params (v1)."""
    return {"conversationId": conversation_id, "hostId": "local", "following": following}


def owner_discovery(conversation_id: str) -> dict[str, Any]:
    """`thread-owner-discovery` params (v1)."""
    return {"hostId": "local", "conversationId": conversation_id}
