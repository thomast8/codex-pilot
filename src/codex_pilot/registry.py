"""Method version registry for Codex Desktop's IPC protocol.

Every request must carry a `version` matching the method's pinned version.
Otherwise the receiving client answers `canHandle: false` during the router's
client-discovery round and the caller gets `no-client-found` -- which looks like
"nobody owns this thread" rather than "your version is wrong". This was verified
live: `thread-owner-discovery` with no version returned no-client-found, and the
identical request with `version: 1` returned success from the real owner.

Source of truth is the `b_` object in the installed app's own Electron bundle
(`app.asar`, `src-DlBR1tzg.js`) -- not the `codex` CLI on PATH, which is a
different build. Here the app ships 0.149.0-alpha.4.3 while the CLI is 0.147.0.
remodex's DESKTOP_IPC_METHOD_VERSIONS agrees with this map and is a secondary
cross-check only.

If a Codex Desktop update bumps a version, calls to that method start failing
with no-client-found on a thread the app visibly owns. Run
`scripts/extract_registry.py` to diff every installed bundle against this map;
`tests/test_registry_drift.py` runs the same check as part of the suite. Doppel
clones carry a patched app.asar, so each bundle is verified separately.
"""

from __future__ import annotations

import uuid
from typing import Any

METHOD_VERSIONS: dict[str, int] = {
    "thread-stream-state-changed": 11,
    "thread-stream-following-changed": 1,
    "thread-stream-following-status-requested": 1,
    "ipc-connection-reset": 1,
    "thread-read-state-changed": 2,
    "thread-archived": 2,
    "thread-unarchived": 1,
    "thread-owner-discovery": 1,
    "thread-follower-start-turn": 2,
    "thread-follower-load-complete-history": 1,
    "thread-follower-compact-thread": 1,
    "thread-follower-steer-turn": 1,
    "thread-follower-interrupt-turn": 4,
    "thread-follower-update-thread-settings": 1,
    "thread-follower-edit-last-user-turn": 2,
    "thread-follower-command-approval-decision": 1,
    "thread-follower-file-approval-decision": 1,
    "thread-follower-permissions-request-approval-response": 1,
    "thread-follower-submit-user-input": 1,
    "thread-follower-submit-mcp-server-elicitation-response": 1,
    "thread-follower-set-queued-follow-ups-state": 1,
    "thread-queued-followups-changed": 1,
}

# The router handles `initialize` itself; it is not in the bundle's method map.
INITIALIZE = "initialize"

INTERRUPT = "thread-follower-interrupt-turn"
INTERRUPT_VERSION_WITHOUT_EXPECTED_TURN = 3


class UnknownMethodError(Exception):
    """Refuse to guess a version -- a wrong one fails as a confusing no-client-found."""


def version_for(method: str, params: dict[str, Any]) -> int:
    """Pinned protocol version for a method, given the params it will carry."""
    if method == INITIALIZE:
        return 1
    if method == INTERRUPT and params.get("expectedTurnId") is None:
        # Bundle function S_ downgrades interrupt to 3 when expectedTurnId is
        # absent or null, so the precondition-free form stays callable.
        return INTERRUPT_VERSION_WITHOUT_EXPECTED_TURN
    try:
        return METHOD_VERSIONS[method]
    except KeyError:
        raise UnknownMethodError(
            f"{method!r} is not in the version registry; re-extract `b_` from app.asar"
        ) from None


def build_request(
    method: str, params: dict[str, Any], target_client_id: str | None = None
) -> dict[str, Any]:
    """Build a router request envelope, with the version the receiver expects."""
    request: dict[str, Any] = {
        "type": "request",
        "requestId": str(uuid.uuid4()),
        "method": method,
        "params": params,
        "version": version_for(method, params),
    }
    if target_client_id is not None:
        request["targetClientId"] = target_client_id
    return request


def build_broadcast(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build a router broadcast envelope (no requestId, no response expected)."""
    return {
        "type": "broadcast",
        "method": method,
        "params": params,
        "version": version_for(method, params),
    }
