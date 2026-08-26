"""Method version registry and request-envelope construction.

Codex Desktop validates the `version` field on every request and refuses one it
does not recognise: the receiving client answers `canHandle: false` and the
router returns `no-client-found`. Verified live -- `thread-owner-discovery` with
no version errored, the identical request with `version: 1` succeeded.
"""

import pytest

from codex_pilot.registry import (
    METHOD_VERSIONS,
    UnknownMethodError,
    build_request,
    version_for,
)


def test_registry_matches_bundle_map_exactly():
    # Verbatim from the app bundle's `b_` object; remodex's
    # DESKTOP_IPC_METHOD_VERSIONS agrees, which is the drift cross-check.
    assert METHOD_VERSIONS == {
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


def test_initialize_is_version_one_even_though_absent_from_the_map():
    assert version_for("initialize", {}) == 1


def test_interrupt_is_version_3_without_expected_turn_id():
    # Bundle function S_: interrupt drops to 3 when expectedTurnId is missing or null.
    assert version_for("thread-follower-interrupt-turn", {"conversationId": "c"}) == 3
    assert (
        version_for(
            "thread-follower-interrupt-turn", {"conversationId": "c", "expectedTurnId": None}
        )
        == 3
    )


def test_interrupt_is_version_4_with_expected_turn_id():
    assert (
        version_for(
            "thread-follower-interrupt-turn", {"conversationId": "c", "expectedTurnId": "t"}
        )
        == 4
    )


def test_stream_state_changed_is_pinned_at_eleven():
    assert version_for("thread-stream-state-changed", {}) == 11


def test_unknown_method_raises_rather_than_defaulting():
    # Guessing a version would silently produce no-client-found at runtime.
    with pytest.raises(UnknownMethodError):
        version_for("thread-follower-invented-method", {})


def test_build_request_shape():
    req = build_request("thread-owner-discovery", {"hostId": "local", "conversationId": "abc"})
    assert req["type"] == "request"
    assert req["method"] == "thread-owner-discovery"
    assert req["params"] == {"hostId": "local", "conversationId": "abc"}
    assert req["version"] == 1
    assert isinstance(req["requestId"], str) and len(req["requestId"]) == 36
    assert "targetClientId" not in req


def test_build_request_includes_target_client_id_when_given():
    req = build_request(
        "thread-follower-steer-turn", {"conversationId": "c"}, target_client_id="w1"
    )
    assert req["targetClientId"] == "w1"


def test_build_request_generates_unique_request_ids():
    a = build_request("thread-owner-discovery", {})
    b = build_request("thread-owner-discovery", {})
    assert a["requestId"] != b["requestId"]


def test_build_request_derives_interrupt_version_from_params():
    without = build_request("thread-follower-interrupt-turn", {"mode": "user-stop"})
    with_turn = build_request(
        "thread-follower-interrupt-turn", {"mode": "user-stop", "expectedTurnId": "t1"}
    )
    assert without["version"] == 3
    assert with_turn["version"] == 4
