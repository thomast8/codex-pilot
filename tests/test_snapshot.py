"""Projection of stream state, exercised against a real captured request.

The pending-request fixture below is a verbatim capture from a live thread that
was blocked on network access, including its `availableDecisions` list.
"""

from __future__ import annotations

from codex_pilot import snapshot

REAL_COMMAND_REQUEST = {
    "method": "item/commandExecution/requestApproval",
    "id": 1865,
    "params": {
        "threadId": "01a03e5e-3216-7592-a0a6-1768f4f1abb5",
        "turnId": "01a03e6c-fa27-7e01-9910-d884680d1203",
        "itemId": "exec-fecc7e28-0fe4-43a3-9f13-282dc491b590",
        "environmentId": "local",
        "reason": "May I rerun the exact requested curl command with network access?",
        "command": "/bin/zsh -lc \"curl -sS -o /dev/null -w '%{http_code}' https://example.com\"",
        "cwd": "/Users/thomastiotto/Documents/Codex/codex-pilot-phaseb",
        "availableDecisions": [
            "accept",
            {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["curl", "-sS"]}},
            "cancel",
        ],
    },
}


def frame(**state):
    base = {
        "threadRuntimeStatus": {"type": "active", "activeFlags": []},
        "requests": [],
        "latestThreadSettings": {},
        "currentPermissions": {},
    }
    base.update(state)
    return {"params": {"change": {"type": "snapshot", "revision": 7, "conversationState": base}}}


def test_parses_a_real_command_request():
    st = snapshot.project(frame(requests=[REAL_COMMAND_REQUEST]))
    assert st is not None
    req = st.pending[0]
    assert req.kind == "command"
    assert req.summary.endswith('https://example.com"')
    assert req.reason.startswith("May I rerun")
    assert req.cwd.endswith("codex-pilot-phaseb")


def test_request_id_keeps_its_integer_type():
    # The app assigns integer ids and echoes them back on answer; stringifying
    # would silently fail to match.
    req = snapshot.project(frame(requests=[REAL_COMMAND_REQUEST])).pending[0]
    assert req.request_id == 1865
    assert isinstance(req.request_id, int)


def test_available_decisions_are_carried_through():
    req = snapshot.project(frame(requests=[REAL_COMMAND_REQUEST])).pending[0]
    labels = {d if isinstance(d, str) else next(iter(d)) for d in req.available_decisions}
    # This request offers no `decline` -- the list is per-request, not global.
    assert labels == {"accept", "acceptWithExecpolicyAmendment", "cancel"}
    assert "decline" not in labels


def test_turn_id_comes_from_the_pending_request():
    st = snapshot.project(frame(requests=[REAL_COMMAND_REQUEST]))
    assert st.turn_id == "01a03e6c-fa27-7e01-9910-d884680d1203"


def test_busy_and_idle():
    assert snapshot.project(frame()).busy is True
    assert snapshot.project(frame(threadRuntimeStatus={"type": "idle"})).idle is True


def test_settings_are_projected():
    st = snapshot.project(
        frame(
            latestThreadSettings={
                "model": "gpt-5.6-terra",
                "effort": "high",
                "serviceTier": "priority",
                "collaborationMode": {"mode": "plan", "settings": {"model": "gpt-5.6-terra"}},
            },
            currentPermissions={"approvalsReviewer": "user"},
        )
    )
    assert (st.model, st.effort, st.service_tier) == ("gpt-5.6-terra", "high", "priority")
    assert st.collaboration_mode == "plan"
    assert st.approvals_reviewer == "user"


def test_unreadable_frame_projects_to_none_not_empty():
    # A read failure must never look like "nothing pending", or an autonomous
    # answerer concludes the thread is waiting on nothing.
    assert snapshot.project(None) is None
    assert snapshot.project({"params": {"change": {}}}) is None


def test_unknown_request_method_is_listed_but_not_answerable():
    st = snapshot.project(frame(requests=[{"id": 9, "method": "item/future/thing", "params": {}}]))
    req = st.pending[0]
    assert req.kind is None
    assert req.answerable is False


def test_request_without_an_id_is_skipped():
    assert snapshot.project(frame(requests=[{"method": "x", "params": {}}])).pending == []
