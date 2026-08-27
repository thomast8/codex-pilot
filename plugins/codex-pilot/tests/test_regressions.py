"""Regressions for defects found by review, each reproducing the original bug.

These all failed before the fix and describe the failure, not the mechanism, so
they keep meaning if the implementation changes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codex_pilot.follow import (
    EVENT_FOLLOW_LOST,
    EVENT_RESYNC,
    EVENT_TURN_COMPLETED,
    FollowManager,
    SeqCounter,
)
from codex_pilot.ipc import STALE_STRIKE_LIMIT, IpcClient
from codex_pilot.snapshot import PatchError, apply_patches, project


class _StrikeOnly(IpcClient):
    """An IpcClient with no socket: only the strike bookkeeping is exercised."""

    def __init__(self) -> None:
        self._last_frame = 0.0
        self._strikes = 0
        self._fatal: BaseException | None = None
        self.closed_calls = 0

    def close(self) -> None:
        self.closed_calls += 1


def _detached_client() -> _StrikeOnly:
    return _StrikeOnly()


FIXTURE = Path(__file__).parent / "fixtures" / "stream_frames.json"
THREAD = "01a03e2b-a106-77a3-add2-913ac3f7336a"


@pytest.fixture
def frames() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def patch_frame(frames: list[dict], base: object) -> dict:
    frame = json.loads(
        json.dumps(next(f for f in frames if f["params"]["change"]["type"] == "patches"))
    )
    frame["params"]["change"]["baseRevision"] = base
    return frame


# -- a gap used to wedge the follow forever -----------------------------------


def test_a_gap_asks_for_a_fresh_snapshot(frames):
    m = FollowManager("default")
    m.follow(THREAD)
    m.handle_frame(frames[0])
    m.handle_frame(patch_frame(frames, 9999))
    # Without a re-request nothing would ever re-seed the state and the follow
    # would stay silent for the rest of the session.
    assert m.take_resync_requests() == [THREAD]


def test_a_broken_stream_does_not_bury_real_events_in_resyncs(frames):
    m = FollowManager("default")
    m.follow(THREAD)
    m.handle_frame(frames[0])
    for _ in range(300):
        m.handle_frame(patch_frame(frames, 9999))
    resyncs = [e for e in m.collect([THREAD])["events"] if e["type"] == EVENT_RESYNC]
    assert len(resyncs) == 1


def test_the_follow_recovers_once_a_snapshot_arrives(frames):
    m = FollowManager("default")
    m.follow(THREAD)
    m.handle_frame(frames[0])
    m.handle_frame(patch_frame(frames, 9999))
    for frame in frames:
        m.handle_frame(frame)
    assert m.state_of(THREAD) is not None
    types = {e["type"] for e in m.collect([THREAD])["events"]}
    assert EVENT_TURN_COMPLETED in types


def test_the_resync_event_reports_the_revision_actually_held(frames):
    m = FollowManager("default")
    m.follow(THREAD)
    m.handle_frame(frames[0])
    held = m._threads[THREAD].revision
    m.handle_frame(patch_frame(frames, 9999))
    resync = next(e for e in m.collect([THREAD])["events"] if e["type"] == EVENT_RESYNC)
    assert resync["data"]["held"] == held


def test_a_patch_without_a_base_revision_is_treated_as_a_gap(frames):
    m = FollowManager("default")
    m.follow(THREAD)
    m.handle_frame(frames[0])
    events = m.handle_frame(patch_frame(frames, None))
    assert [e.type for e in events] == [EVENT_RESYNC]


# -- follow_lost used to repeat forever ---------------------------------------


def test_follow_lost_is_emitted_once_per_transition(frames):
    m = FollowManager("default")
    m.follow(THREAD)
    for _ in range(50):
        m.lost(THREAD, "Codex Desktop is not reachable")
    lost = [e for e in m.collect([THREAD])["events"] if e["type"] == EVENT_FOLLOW_LOST]
    assert len(lost) == 1


def test_a_different_reason_is_reported_again(frames):
    m = FollowManager("default")
    m.follow(THREAD)
    m.lost(THREAD, "reason one")
    m.lost(THREAD, "reason two")
    lost = [e for e in m.collect([THREAD])["events"] if e["type"] == EVENT_FOLLOW_LOST]
    assert len(lost) == 2


# -- cursors used to mask a quieter instance ----------------------------------


def test_shared_sequence_keeps_one_cursor_meaningful():
    seq = SeqCounter()
    busy, quiet = FollowManager("a", seq=seq), FollowManager("b", seq=seq)
    busy.follow("t-a")
    quiet.follow("t-b")
    for i in range(6):
        busy._emit(busy._threads["t-a"], "synthetic", {"i": i})
    quiet._emit(quiet._threads["t-b"], "synthetic", {"i": 0})
    seqs = [e.seq for e in busy._threads["t-a"].events] + [
        e.seq for e in quiet._threads["t-b"].events
    ]
    # Duplicated numbers across managers made the quiet instance's later events
    # fall below a cursor advanced by the busy one.
    assert len(seqs) == len(set(seqs))
    assert quiet._threads["t-b"].events[0].seq > busy._threads["t-a"].events[-1].seq


# -- dropped used to re-alarm forever -----------------------------------------


def test_dropped_reports_the_delta_not_a_lifetime_total():
    m = FollowManager("default")
    m.follow(THREAD)
    tracked = m._threads[THREAD]
    for i in range(tracked.events.maxlen + 10):
        m._emit(tracked, "synthetic", {"i": i})
    first = m.collect([THREAD])
    assert first["dropped"] == 10
    assert m.collect([THREAD], after=first["cursor"])["dropped"] == 0


# -- a malformed frame used to kill the reader --------------------------------


@pytest.mark.parametrize(
    "state",
    [
        {"latestThreadSettings": "not a dict"},
        {"currentPermissions": 7},
        {"threadRuntimeStatus": "active"},
        {"turnHistory": "nope"},
        {"latestThreadSettings": {"collaborationMode": "plan"}},
    ],
)
def test_a_scalar_where_an_object_was_expected_does_not_raise(state):
    # A patch can replace any key with any value, and an exception here used to
    # kill the pump for the whole instance with no follow_lost emitted.
    assert project({"params": {"change": {"type": "snapshot", "conversationState": state}}})


# -- patches used to diverge silently -----------------------------------------


def test_replace_on_a_missing_path_is_an_error_not_an_invention():
    # Fabricating the path made our state disagree with the app's, with nothing
    # to notice the divergence.
    with pytest.raises(PatchError):
        apply_patches({"a": 1}, [{"op": "replace", "path": ["x", "y"], "value": 1}])


def test_remove_on_a_missing_path_does_not_add_keys():
    with pytest.raises(PatchError):
        apply_patches({"a": 1}, [{"op": "remove", "path": ["turnHistory", "history", "x"]}])


def test_remove_with_a_non_integer_index_on_a_list_is_an_error():
    with pytest.raises(PatchError):
        apply_patches({"requests": [{"id": 1}]}, [{"op": "remove", "path": ["requests", "1"]}])


def test_add_may_still_create_its_path():
    out = apply_patches({}, [{"op": "add", "path": ["turnHistory", "history"], "value": {}}])
    assert out["turnHistory"]["history"] == {}


def test_list_index_out_of_range_is_an_error():
    with pytest.raises(PatchError):
        apply_patches({"a": [1]}, [{"op": "replace", "path": ["a", 5, "b"], "value": 1}])


# -- state_of used to lose the revision ---------------------------------------


def test_state_of_reports_the_held_revision(frames):
    m = FollowManager("default")
    m.follow(THREAD)
    m.handle_frame(frames[0])
    assert m.state_of(THREAD).revision == m._threads[THREAD].revision


# -- long poll --------------------------------------------------------------


def test_long_poll_still_returns_promptly_when_idle():
    m = FollowManager("default")
    m.follow(THREAD)
    started = time.monotonic()
    assert m.collect([THREAD], wait_seconds=0.4)["events"] == []
    assert time.monotonic() - started >= 0.3


# -- the app-wedge episode ----------------------------------------------------
#
# One morning Codex Desktop froze mid-turn behind a macOS update dialog. Three
# separate defects each hid it, and two threads sat wedged for twenty minutes.
# These describe what a supervisor could not see, not how it is now seen.


def test_a_connection_that_answers_nothing_is_eventually_given_up_on():
    """Every send_message failed while list_threads and focus_thread worked.

    Those two read the disk and shell out, so they never proved the socket was
    alive -- and the socket never reported itself closed, because a frozen app
    holds it open. Only killing the server process by hand recovered it.
    """
    client = _detached_client()

    retired = [client._record_silent_timeout(time.monotonic()) for _ in range(STALE_STRIKE_LIMIT)]
    assert retired[-1] is True
    assert client.closed_calls == 1


def test_a_live_connection_is_never_given_up_on_for_a_slow_thread():
    """An unmounted thread costs the router its full discovery timeout.

    Counting that as evidence about the connection would tear down a healthy
    one every time somebody asked about a thread the app is not rendering.
    """
    client = _detached_client()

    for _ in range(10):
        sent_at = time.monotonic()
        client._last_frame = sent_at + 0.001  # a frame landed while we waited
        assert client._record_silent_timeout(sent_at) is False
    assert client.closed_calls == 0


def test_an_empty_following_list_no_longer_looks_like_a_quiet_thread():
    """collect_events answered `following: []` while a thread held an approval.

    The registry had gone with the server process, so there was nothing to
    re-arm and nothing in the response to say the thread was unwatched.
    """
    manager = FollowManager("default")
    got = manager.collect([THREAD])

    assert got["following"] == []
    assert got["threads"][THREAD]["health"] == "not_following"
    assert got["threads"][THREAD]["pending_known"] is False


def test_a_follow_that_survived_a_reconnect_asks_for_a_new_snapshot(frames):
    """Registrations outlive a connection; subscriptions do not.

    `lost()` reported the drop but queued no resync, and the pump only
    re-subscribes threads that asked for one -- so the follow stayed listed and
    permanently silent.
    """
    manager = FollowManager("default")
    manager.follow(THREAD)
    manager.handle_frame(frames[0])
    manager.lost(THREAD, "IPC connection closed")

    manager.resync_all("reconnected")
    assert manager.take_resync_requests() == [THREAD]


def test_unreadable_stream_state_is_never_reported_as_nothing_pending(tmp_path):
    """`state: null` and "no approvals" were the same answer.

    The rollout cannot supply the pending request -- no record type exists for
    one -- but it can say the thread was left inside a turn.
    """
    from codex_pilot import transcript

    roll = tmp_path / "r.jsonl"
    roll.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-27T10:45:36.919Z",
                "type": "event_msg",
                "payload": {"type": "task_started"},
            }
        )
        + "\n"
    )
    phase = transcript.rollout_turn_phase(roll)
    assert phase is not None and phase.mid_turn
