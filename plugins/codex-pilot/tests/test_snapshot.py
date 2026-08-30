"""Projection of stream state, exercised against a real captured request.

The pending-request fixture below is a verbatim capture from a live thread that
was blocked on network access, including its `availableDecisions` list.
"""

from __future__ import annotations

import pytest

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


# -- active turn id -----------------------------------------------------------


def test_turn_id_from_a_confirmed_turn_key():
    st = snapshot.project(
        frame(
            turnHistory={
                "history": {
                    "entitiesByKey": {
                        "turn:01a03e33-76e3-7142-a99b-e3cccf85e540": {"status": "inProgress"}
                    }
                }
            }
        )
    )
    assert st.turn_id == "01a03e33-76e3-7142-a99b-e3cccf85e540"


def test_turn_id_field_beats_the_key():
    st = snapshot.project(
        frame(
            turnHistory={
                "history": {
                    "entitiesByKey": {"turn:stale": {"status": "inProgress", "turnId": "real-id"}}
                }
            }
        )
    )
    assert st.turn_id == "real-id"


def test_optimistic_local_turn_has_no_id_yet():
    # A `tail:` entry is a turn the window created before the server assigned
    # an id; null means "not yet", not "no turn".
    st = snapshot.project(
        frame(
            turnHistory={
                "history": {
                    "entitiesByKey": {
                        "tail:3:local:074b5755": {"status": "inProgress", "turnId": None}
                    }
                }
            }
        )
    )
    assert st.turn_id is None
    assert st.busy is True


def test_completed_turns_are_not_reported_as_active():
    st = snapshot.project(
        frame(turnHistory={"history": {"entitiesByKey": {"turn:done": {"status": "completed"}}}})
    )
    assert st.turn_id is None


# -- the latest turn, as the app's steer gate sees it --------------------------


def canonical(*islands, entities=None):
    """A canonical turn history, in the shape the app broadcasts."""
    return {
        "kind": "canonical",
        "history": {"islands": list(islands), "entitiesByKey": dict(entities or {})},
    }


def island(*keys, newer="exhausted"):
    return {"entries": [{"key": k, "value": k} for k in keys], "newerBoundary": {"status": newer}}


def test_latest_turn_is_the_last_entry_of_the_closed_tail_island():
    st = snapshot.project(
        frame(
            turnHistory=canonical(
                island("a", "b"),
                entities={
                    "a": {"status": "completed", "turnId": "first"},
                    "b": {"status": "inProgress", "turnId": "second"},
                },
            )
        )
    )
    assert st.latest_turn.turn_id == "second"
    assert st.latest_turn.placeholder is False


def test_an_id_less_turn_in_progress_is_a_placeholder():
    # The state that makes the app refuse to steer: in progress, but the
    # server never assigned an id.
    st = snapshot.project(
        frame(
            turnHistory=canonical(
                island("orphan"),
                entities={
                    "orphan": {
                        "status": "inProgress",
                        "turnId": None,
                        "turnStartedAtMs": 1_788_111_798_512,
                    }
                },
            )
        )
    )
    assert st.latest_turn.placeholder is True
    assert st.latest_turn.age_seconds(1_788_111_858.512) == pytest.approx(60.0)


def test_a_placeholder_behind_a_real_turn_is_not_the_latest():
    # The captured shape from the live thread: the orphan is still in the
    # history, but a real turn landed after it, so the app steers that one.
    st = snapshot.project(
        frame(
            turnHistory=canonical(
                island("orphan", "real"),
                entities={
                    "orphan": {"status": "inProgress", "turnId": None},
                    "real": {"status": "inProgress", "turnId": "01a053cf"},
                },
            )
        )
    )
    assert st.latest_turn.placeholder is False
    assert st.latest_turn.turn_id == "01a053cf"


def test_an_open_tail_island_falls_back_to_the_last_entry_overall():
    # `av` only returns the tail island when its newer boundary is exhausted;
    # otherwise `lv` flattens every island and takes the last entry, which is
    # the same element whenever that island is non-empty.
    st = snapshot.project(
        frame(
            turnHistory=canonical(
                island("a"),
                island("b", newer="unknown"),
                entities={"a": {"status": "completed"}, "b": {"status": "inProgress"}},
            )
        )
    )
    assert st.latest_turn.status == "inProgress"


def test_an_empty_closed_tail_island_reads_as_no_latest_turn():
    # The one place the two branches disagree: `av` returns the empty island,
    # `entries.at(-1)` is undefined, and `lv` returns null rather than looking
    # further back.
    st = snapshot.project(
        frame(
            turnHistory=canonical(island("a"), island(), entities={"a": {"status": "inProgress"}})
        )
    )
    assert st.latest_turn is None


def test_an_empty_open_tail_island_skips_to_the_island_before_it():
    st = snapshot.project(
        frame(
            turnHistory=canonical(
                island("a"), island(newer="unknown"), entities={"a": {"status": "inProgress"}}
            )
        )
    )
    assert st.latest_turn.status == "inProgress"


def test_a_non_canonical_history_reads_the_plain_turn_list():
    st = snapshot.project(frame(turns=[{"status": "completed"}, {"status": "inProgress"}]))
    assert st.latest_turn.status == "inProgress"


def test_an_explicitly_uncanonical_history_reads_the_plain_turn_list_too():
    # `kind` is the discriminator, so a history present under some other kind
    # has to route the same way an absent one does -- otherwise the islands
    # branch would read a shape that is not there.
    st = snapshot.project(
        frame(
            turnHistory={"kind": "something-else", "history": {"islands": []}},
            turns=[{"status": "inProgress", "turnId": "plain"}],
        )
    )
    assert st.latest_turn.turn_id == "plain"


def test_an_entry_pointing_at_a_missing_entity_reads_as_unknown():
    # The app resolves the entry through `entitiesByKey` and stops at whatever
    # it finds, including nothing; it does not walk further back. Neither do
    # we, and None reads as "cannot tell" at the caller rather than as a turn.
    st = snapshot.project(
        frame(turnHistory=canonical(island("a", "gone"), entities={"a": {"status": "inProgress"}}))
    )
    assert st.latest_turn is None


def test_turn_id_and_latest_turn_name_different_turns():
    # The disagreement the two fields exist for: a real turn still running with
    # an id-less one appended behind it. `turn_id` finds the one worth stopping,
    # `latest_turn` the one the app would steer.
    st = snapshot.project(
        frame(
            turnHistory=canonical(
                island("real", "orphan"),
                entities={
                    "real": {"status": "inProgress", "turnId": "01a053d9"},
                    "orphan": {"status": "inProgress", "turnId": None},
                },
            )
        )
    )
    assert st.turn_id == "01a053d9"
    assert st.latest_turn.turn_id is None
    assert st.latest_turn.placeholder is True


def test_no_history_at_all_is_no_latest_turn_rather_than_a_guess():
    assert snapshot.project(frame()).latest_turn is None


def test_a_turn_with_no_start_time_has_no_age_rather_than_a_default():
    # Age decides whether a placeholder is stuck or merely young, so a missing
    # timestamp must read as "cannot tell", never as zero.
    st = snapshot.project(
        frame(turnHistory=canonical(island("a"), entities={"a": {"status": "inProgress"}}))
    )
    assert st.latest_turn.age_seconds(1_788_111_858.512) is None
