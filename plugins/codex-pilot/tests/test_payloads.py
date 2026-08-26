"""Literal-JSON assertions for every pinned payload shape.

These are the shapes decoded from the installed app bundle. If a Codex Desktop
update changes one, this is where it should fail loudly rather than silently at
runtime against a real thread.
"""

from __future__ import annotations

import pytest

from codex_pilot import payloads
from codex_pilot.registry import version_for

CONV = "01a03e2b-a106-77a3-add2-913ac3f7336a"


# -- input --------------------------------------------------------------------


def test_text_input_is_an_array_of_user_input():
    assert payloads.text_input("hello") == [{"type": "text", "text": "hello", "text_elements": []}]


# -- start turn ---------------------------------------------------------------


def test_start_turn_literal_shape():
    got = payloads.start_turn(CONV, "hello", client_user_message_id="fixed-id")
    assert got == {
        "conversationId": CONV,
        "turnStart": {
            "request": {
                "threadId": CONV,
                "input": [{"type": "text", "text": "hello", "text_elements": []}],
                "clientUserMessageId": "fixed-id",
            },
            "context": {
                "attachments": [],
                "commentAttachments": [],
                "inheritThreadSettings": True,
            },
        },
    }


def test_start_turn_repeats_the_conversation_id_as_thread_id():
    # The renderer throws "Turn request thread does not match the conversation"
    # when these differ, so the duplication is load-bearing.
    got = payloads.start_turn(CONV, "hi")
    assert got["turnStart"]["request"]["threadId"] == got["conversationId"]


def test_start_turn_generates_a_client_user_message_id():
    got = payloads.start_turn(CONV, "hi")
    assert len(got["turnStart"]["request"]["clientUserMessageId"]) == 36


def test_start_turn_omits_optional_fields_when_unset():
    request = payloads.start_turn(CONV, "hi")["turnStart"]["request"]
    assert "cwd" not in request
    assert "model" not in request


def test_start_turn_includes_cwd_and_model_when_given():
    request = payloads.start_turn(CONV, "hi", cwd="/w/tree", model="gpt-5.6-terra")["turnStart"][
        "request"
    ]
    assert request["cwd"] == "/w/tree"
    assert request["model"] == "gpt-5.6-terra"


# -- steer --------------------------------------------------------------------


def test_steer_turn_literal_shape():
    got = payloads.steer_turn(
        CONV, "actually, do X", cwd="/w/tree", client_user_message_id="fixed-id"
    )
    assert got == {
        "conversationId": CONV,
        "input": [{"type": "text", "text": "actually, do X", "text_elements": []}],
        "clientUserMessageId": "fixed-id",
        "attachments": [],
        "additionalContext": None,
        "serviceTier": None,
        "restoreMessage": {
            "cwd": "/w/tree",
            "context": {"workspaceRoots": ["/w/tree"], "collaborationMode": None},
        },
    }


def test_steer_turn_always_includes_restore_message():
    # The renderer dereferences restoreMessage.cwd and
    # restoreMessage.context.workspaceRoots without a guard; omitting it fails
    # inside the app, not at the protocol layer.
    got = payloads.steer_turn(CONV, "x")
    assert got["restoreMessage"]["cwd"] is None
    assert got["restoreMessage"]["context"]["workspaceRoots"] == []


def test_steer_turn_does_not_send_expected_turn_id():
    # The owning window derives it from its own active turn; the IPC handler
    # never reads one.
    assert "expectedTurnId" not in payloads.steer_turn(CONV, "x")


# -- interrupt ----------------------------------------------------------------


def test_interrupt_without_expected_turn_id_is_version_3():
    params = payloads.interrupt_turn(CONV)
    assert params == {"conversationId": CONV, "mode": "user-stop"}
    assert version_for("thread-follower-interrupt-turn", params) == 3


def test_interrupt_with_expected_turn_id_is_version_4():
    params = payloads.interrupt_turn(CONV, expected_turn_id="turn-7")
    assert params == {"conversationId": CONV, "mode": "user-stop", "expectedTurnId": "turn-7"}
    assert version_for("thread-follower-interrupt-turn", params) == 4


def test_interrupt_mode_can_be_overridden():
    assert payloads.interrupt_turn(CONV, mode="descendant-cleanup")["mode"] == "descendant-cleanup"


# -- respond ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "method"),
    [
        ("command", "thread-follower-command-approval-decision"),
        ("file", "thread-follower-file-approval-decision"),
        ("permissions", "thread-follower-permissions-request-approval-response"),
        ("user_input", "thread-follower-submit-user-input"),
        ("elicitation", "thread-follower-submit-mcp-server-elicitation-response"),
    ],
)
def test_each_kind_routes_to_its_method(kind, method):
    payload = {"decision": "accept"} if kind in payloads.DECISION_KINDS else {"response": {}}
    got_method, _ = payloads.respond(CONV, "req-1", kind, **payload)
    assert got_method == method
    assert version_for(got_method, {}) == 1


def test_approval_kinds_carry_decision():
    _, params = payloads.respond(CONV, "req-1", "command", decision="accept")
    assert params == {"conversationId": CONV, "requestId": "req-1", "decision": "accept"}


def test_input_kinds_carry_response():
    _, params = payloads.respond(CONV, "req-1", "user_input", response={"answers": {}})
    assert params == {"conversationId": CONV, "requestId": "req-1", "response": {"answers": {}}}


def test_unknown_kind_is_rejected_with_the_valid_list():
    with pytest.raises(ValueError, match="unknown request kind"):
        payloads.respond(CONV, "req-1", "nonsense", decision="accept")


def test_decision_kind_without_a_decision_is_rejected():
    with pytest.raises(ValueError, match="need a `decision`"):
        payloads.respond(CONV, "req-1", "command")


def test_response_kind_without_a_response_is_rejected():
    with pytest.raises(ValueError, match="need a `response`"):
        payloads.respond(CONV, "req-1", "elicitation")


def test_request_method_map_covers_every_respond_kind():
    assert set(payloads.REQUEST_METHOD_TO_KIND.values()) == set(payloads.RESPOND_METHODS)


def test_accept_for_session_is_flagged_as_a_persistent_grant():
    # Not a per-request answer: it stops Codex prompting for matching commands
    # for the rest of the session.
    assert "acceptForSession" in payloads.PERSISTENT_GRANTS
    assert payloads.ACCEPT not in payloads.PERSISTENT_GRANTS


# -- broadcasts / discovery ---------------------------------------------------


def test_follow_literal_shape():
    assert payloads.follow(CONV) == {
        "conversationId": CONV,
        "hostId": "local",
        "following": True,
    }
    assert payloads.follow(CONV, following=False)["following"] is False


def test_owner_discovery_literal_shape():
    assert payloads.owner_discovery(CONV) == {"hostId": "local", "conversationId": CONV}


# -- thread settings ----------------------------------------------------------


def test_collaboration_mode_carries_required_settings():
    # The app-server requires {mode, settings} with settings.model; sending
    # {"mode": "plan"} alone is rejected as `missing field 'settings'`.
    got = payloads.collaboration_mode("plan", model="gpt-5.6-terra", reasoning_effort="high")
    assert got == {
        "mode": "plan",
        "settings": {"model": "gpt-5.6-terra", "reasoning_effort": "high"},
    }


def test_collaboration_mode_rejects_unknown_mode():
    with pytest.raises(ValueError, match="mode must be one of"):
        payloads.collaboration_mode("turbo", model="m")


def test_update_thread_settings_literal_shape():
    got = payloads.update_thread_settings(CONV, {"effort": "high", "serviceTier": "priority"})
    assert got == {
        "conversationId": CONV,
        "threadSettings": {"effort": "high", "serviceTier": "priority"},
    }


def test_update_thread_settings_rejects_unknown_fields():
    with pytest.raises(ValueError, match="unknown thread settings"):
        payloads.update_thread_settings(CONV, {"nonsense": 1})


def test_update_thread_settings_rejects_bare_collaboration_mode():
    with pytest.raises(ValueError, match="settings.model"):
        payloads.update_thread_settings(CONV, {"collaborationMode": {"mode": "plan"}})


def test_update_thread_settings_rejects_unknown_service_tier():
    with pytest.raises(ValueError, match="serviceTier must be one of"):
        payloads.update_thread_settings(CONV, {"serviceTier": "ludicrous"})


def test_compact_thread_literal_shape():
    assert payloads.compact_thread(CONV) == {"conversationId": CONV}
