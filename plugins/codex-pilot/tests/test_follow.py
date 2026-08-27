"""Follow subsystem, replayed against a real captured stream.

`tests/fixtures/stream_frames.json` is a verbatim capture of one live turn: two
snapshots and 23 patch frames, revision chain intact. Replaying it is the only
honest way to test patch application, since the format is undocumented.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from codex_pilot.follow import (
    EVENT_FOLLOW_LOST,
    EVENT_RESYNC,
    EVENT_TURN_COMPLETED,
    FollowManager,
)
from codex_pilot.snapshot import PatchError, apply_patches

FIXTURE = Path(__file__).parent / "fixtures" / "stream_frames.json"
THREAD = "01a03e2b-a106-77a3-add2-913ac3f7336a"


@pytest.fixture
def frames() -> list[dict]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def manager() -> FollowManager:
    m = FollowManager("default")
    m.follow(THREAD)
    return m


# -- patch application --------------------------------------------------------


def test_replace_at_a_nested_path():
    state = {"a": {"b": {"c": 1}}}
    out = apply_patches(state, [{"op": "replace", "path": ["a", "b", "c"], "value": 2}])
    assert out["a"]["b"]["c"] == 2


def test_add_creates_a_missing_key():
    out = apply_patches({}, [{"op": "add", "path": ["turnHistory", "x"], "value": 1}])
    assert out["turnHistory"]["x"] == 1


def test_remove_deletes_a_key():
    out = apply_patches({"a": {"b": 1}}, [{"op": "remove", "path": ["a", "b"]}])
    assert "b" not in out["a"]


def test_original_state_is_not_mutated():
    state = {"a": {"b": 1}}
    apply_patches(state, [{"op": "replace", "path": ["a", "b"], "value": 99}])
    assert state["a"]["b"] == 1


def test_a_failed_patch_leaves_nothing_half_applied():
    # Half-applied state looks current and is not, which is worse than stale.
    state = {"a": 1}
    with pytest.raises(PatchError):
        apply_patches(state, [{"op": "explode", "path": ["a"], "value": 2}])
    assert state == {"a": 1}


def test_unknown_op_is_rejected():
    with pytest.raises(PatchError):
        apply_patches({}, [{"op": "frobnicate", "path": ["a"], "value": 1}])


def test_pathless_patch_is_rejected():
    with pytest.raises(PatchError):
        apply_patches({}, [{"op": "replace", "value": 1}])


# -- replay -------------------------------------------------------------------


def test_replaying_the_capture_keeps_the_revision_chain(manager, frames):
    for frame in frames:
        manager.handle_frame(frame)
    tracked = manager._threads[THREAD]
    final_revision = frames[-1]["params"]["change"]["revision"]
    assert tracked.revision == final_revision
    assert tracked.state is not None


def test_replay_never_resyncs_on_a_clean_chain(manager, frames):
    produced = []
    for frame in frames:
        produced.extend(manager.handle_frame(frame))
    assert not [e for e in produced if e.type == EVENT_RESYNC]


def test_replay_reports_the_turn_completing(manager, frames):
    produced = []
    for frame in frames:
        produced.extend(manager.handle_frame(frame))
    # The signal orchestration actually needs: this thread went idle.
    assert any(e.type == EVENT_TURN_COMPLETED for e in produced)


def test_frames_for_unfollowed_threads_are_ignored(frames):
    m = FollowManager("default")
    assert m.handle_frame(frames[0]) == []


# -- gaps ---------------------------------------------------------------------


def test_a_revision_gap_resyncs_instead_of_applying(manager, frames):
    snapshot_frame = next(f for f in frames if f["params"]["change"]["type"] == "snapshot")
    manager.handle_frame(snapshot_frame)
    stale = json.loads(
        json.dumps(next(f for f in frames if f["params"]["change"]["type"] == "patches"))
    )
    stale["params"]["change"]["baseRevision"] = 9999
    events = manager.handle_frame(stale)
    assert [e.type for e in events] == [EVENT_RESYNC]
    assert manager._threads[THREAD].state is None


def test_a_failed_patch_reports_the_revision_it_held(manager, frames):
    """`held` is the one value worth having when debugging a gap, and the
    PatchError branch is the one that does not pass it in itself."""
    snapshot_frame = next(f for f in frames if f["params"]["change"]["type"] == "snapshot")
    manager.handle_frame(snapshot_frame)
    held = manager._threads[THREAD].revision
    assert held is not None

    broken = json.loads(
        json.dumps(next(f for f in frames if f["params"]["change"]["type"] == "patches"))
    )
    broken["params"]["change"]["baseRevision"] = held
    broken["params"]["change"]["patches"] = [{"op": "replace", "path": [], "value": 1}]

    events = manager.handle_frame(broken)

    assert [e.type for e in events] == [EVENT_RESYNC]
    assert events[0].data["held"] == held


def test_patches_before_any_snapshot_resync(manager, frames):
    patch_frame = next(f for f in frames if f["params"]["change"]["type"] == "patches")
    assert [e.type for e in manager.handle_frame(patch_frame)] == [EVENT_RESYNC]


def test_follow_lost_clears_state(manager, frames):
    manager.handle_frame(frames[0])
    manager.lost(THREAD, "app closed")
    collected = manager.collect([THREAD])
    assert collected["events"][-1]["type"] == EVENT_FOLLOW_LOST
    assert manager.state_of(THREAD) is None


# -- delivery -----------------------------------------------------------------


def test_collect_advances_a_cursor(manager, frames):
    for frame in frames:
        manager.handle_frame(frame)
    first = manager.collect([THREAD])
    assert first["events"]
    again = manager.collect([THREAD], after=first["cursor"])
    assert again["events"] == []


def test_collect_returns_immediately_when_events_exist(manager, frames):
    for frame in frames:
        manager.handle_frame(frame)
    started = time.monotonic()
    manager.collect([THREAD], wait_seconds=30)
    assert time.monotonic() - started < 1.0


def test_long_poll_wakes_on_a_new_event(manager, frames):
    def feed():
        time.sleep(0.3)
        for frame in frames[:6]:
            manager.handle_frame(frame)

    threading.Thread(target=feed, daemon=True).start()
    started = time.monotonic()
    result = manager.collect([THREAD], wait_seconds=10)
    assert result["events"]
    assert time.monotonic() - started < 5.0


def test_long_poll_gives_up_at_the_timeout(manager):
    started = time.monotonic()
    result = manager.collect([THREAD], wait_seconds=0.5)
    assert result["events"] == []
    assert time.monotonic() - started >= 0.4


def test_buffer_is_bounded_and_reports_drops(manager):
    tracked = manager._threads[THREAD]
    for i in range(tracked.events.maxlen + 25):
        manager._emit(tracked, "synthetic", {"i": i})
    assert len(tracked.events) == tracked.events.maxlen
    assert manager.collect([THREAD])["dropped"] == 25


def test_unfollow_stops_tracking(manager):
    manager.unfollow(THREAD)
    assert manager.followed == []
    assert manager.is_following(THREAD) is False


# -- gap recovery after a reconnect ----------------------------------------
#
# When the IPC connection is replaced, the app has no record of our
# subscriptions. Recovery has to survive the `awaiting_snapshot` guard, which
# is exactly the state a follow is in when the connection dies mid-gap.


def _open_a_gap(manager: FollowManager, frames: list[dict]) -> None:
    """Feed a patch with no snapshot behind it, which is a revision gap."""
    patch = next(f for f in frames if f["params"]["change"]["type"] == "patches")
    manager.handle_frame(patch)


def test_resync_all_requeues_a_thread_already_awaiting_a_snapshot(manager, frames):
    _open_a_gap(manager, frames)
    assert manager.take_resync_requests() == [THREAD]
    # Second gap while already awaiting: deliberately does not re-queue, which
    # is the wedge a reconnect has to break.
    _open_a_gap(manager, frames)
    assert manager.take_resync_requests() == []

    produced = manager.resync_all("ipc reconnected")

    assert manager.take_resync_requests() == [THREAD]
    assert [e.type for e in produced] == [EVENT_RESYNC]
    assert produced[0].data["reason"] == "ipc reconnected"


def test_resync_all_emits_a_collectable_event_so_silence_is_never_ambiguous(manager, frames):
    manager.resync_all("ipc reconnected")
    collected = manager.collect()
    assert [e["type"] for e in collected["events"]] == [EVENT_RESYNC]


def test_resync_all_on_nothing_followed_is_a_no_op():
    empty = FollowManager("default")
    assert empty.resync_all("ipc reconnected") == []
    assert empty.take_resync_requests() == []


def test_requeue_resync_restores_ids_a_failed_broadcast_took(manager, frames):
    _open_a_gap(manager, frames)
    taken = manager.take_resync_requests()
    assert taken == [THREAD]

    manager.requeue_resync(taken)

    assert manager.take_resync_requests() == [THREAD]


def test_requeue_resync_does_not_duplicate_an_id_still_queued(manager, frames):
    _open_a_gap(manager, frames)
    manager.requeue_resync([THREAD])
    assert manager.take_resync_requests() == [THREAD]

def test_health_reports_pending_when_the_state_is_readable(manager, frames):
    manager.handle_frame(frames[0])
    health = manager.health_of(THREAD)
    assert health["health"] == "ok"
    assert health["pending_known"] is True
    assert health["pending"] == []


def test_health_never_reports_an_unreadable_thread_as_nothing_pending(manager):
    """The whole defect in one assertion: absence of state is not absence of asks."""
    manager.lost(THREAD, "IPC connection closed")
    health = manager.health_of(THREAD)
    assert health["health"] == "lost"
    assert health["pending"] == []
    assert health["pending_known"] is False


def test_health_of_an_unknown_thread_says_so(manager):
    health = manager.health_of("never-followed")
    assert health["health"] == "not_following"
    assert health["pending_known"] is False


def test_collect_only_answers_for_threads_it_actually_tracks(manager, frames):
    """A manager that has never heard of a thread must not answer for it.

    Health is merged flat across instances, so a confident `not_following` from
    the wrong instance would overwrite the truth from the right one. Session
    fills in the genuinely-unknown ids.
    """
    manager.handle_frame(frames[0])
    got = manager.collect([THREAD, "never-followed"])
    assert got["threads"][THREAD]["health"] == "ok"
    assert "never-followed" not in got["threads"]


def test_a_follow_awaiting_a_snapshot_keeps_asking(manager, frames):
    """Asking once is not enough when the app was not ready to answer.

    Verified live: after an app restart the thread was not reopened at all, so
    the re-subscribe went out to an app that would never answer it. The
    awaiting-snapshot latch then stopped anything asking again, leaving the
    follow silent for good once the thread did come back.
    """
    manager.handle_frame(frames[0])
    manager.resync_all("reconnected")
    assert manager.take_resync_requests() == [THREAD]

    # Nothing came back, so it must be asked again -- but not on every tick.
    assert manager.stale_awaiting(older_than=60.0) == []
    assert manager.stale_awaiting(older_than=0.0) == [THREAD]


def test_a_follow_that_got_its_snapshot_stops_asking(manager, frames):
    manager.handle_frame(frames[0])
    manager.resync_all("reconnected")
    manager.take_resync_requests()

    manager.handle_frame(frames[0])
    assert manager.stale_awaiting(older_than=0.0) == []
    assert manager.health_of(THREAD)["health"] == "ok"


def test_retrying_a_resync_does_not_flood_the_event_buffer(manager, frames):
    """The latch exists to stop resync spam evicting real events. Keep that."""
    manager.handle_frame(frames[0])
    before = len(manager.collect()["events"])
    manager.resync_all("reconnected")
    for _ in range(20):
        manager.stale_awaiting(older_than=0.0)
    after = len(manager.collect()["events"])
    assert after - before == 1


def test_a_reconnect_requeues_even_a_follow_already_awaiting(manager, frames):
    """The reconnect path went through `_begin_resync`, which queues nothing
    when the awaiting latch is already set — so a gap followed by a reconnect
    re-subscribed nothing at all, and the retry stamp was pushed out on top."""
    manager.handle_frame(frames[0])
    gap = {
        "params": {
            "conversationId": THREAD,
            "change": {"type": "patches", "baseRevision": 999, "revision": 1000, "patches": []},
        }
    }
    manager.handle_frame(gap)
    assert manager.take_resync_requests() == [THREAD]

    # Now the connection is replaced. This must ask again.
    manager.resync_all("reconnected")
    assert manager.take_resync_requests() == [THREAD]


def test_a_brand_new_follow_is_retried_like_any_other():
    """A fresh follow awaits its first snapshot as surely as one recovering from
    a gap, and an unmounted thread never answers the first subscribe."""
    fresh = FollowManager("default")
    fresh.follow("brand-new")
    assert fresh.stale_awaiting(older_than=0.0) == ["brand-new"]


def test_a_detached_run_is_not_reported_as_unfollowed(manager):
    """`not_following` is documented as "nothing will ever arrive"; a detached
    run's completion arrives on this very stream."""
    manager.emit_external("run-1", EVENT_TURN_COMPLETED, {"route": "detached"})
    health = manager.health_of("run-1")
    assert health["health"] == "detached"
    assert health["pending_known"] is False


# The verbatim live capture from tests/test_snapshot.py: an exec approval that a
# real thread raised when it was blocked on network access.
REAL_COMMAND_REQUEST = {
    "method": "item/commandExecution/requestApproval",
    "id": 1865,
    "params": {
        "threadId": THREAD,
        "turnId": "01a03e6c-fa27-7e01-9910-d884680d1203",
        "itemId": "exec-fecc7e28-0fe4-43a3-9f13-282dc491b590",
        "reason": "May I rerun the exact requested curl command with network access?",
        "command": '/bin/zsh -lc "curl -sS https://example.com"',
        "cwd": "/tmp/x",
        "availableDecisions": ["accept", "cancel"],
    },
}


def snapshot_frame(requests: list[dict]) -> dict:
    return {
        "params": {
            "conversationId": THREAD,
            "change": {
                "type": "snapshot",
                "revision": 1,
                "conversationState": {
                    "threadRuntimeStatus": {"type": "active"},
                    "requests": requests,
                },
            },
        }
    }


def test_a_pending_approval_reaches_the_caller_intact(manager):
    """The headline promise: a blocked thread is visible without polling.

    Every other health assertion checks an empty pending list, so the serializer
    could have returned anything at all for a real request.
    """
    manager.handle_frame(snapshot_frame([REAL_COMMAND_REQUEST]))
    health = manager.health_of(THREAD)

    assert health["health"] == "ok"
    assert health["pending_known"] is True
    (pending,) = health["pending"]
    # The id is an integer and is echoed straight back on answer; stringifying
    # it silently fails to match.
    assert pending["request_id"] == 1865
    assert pending["kind"] == "command"
    assert pending["answerable"] is True
    assert pending["available_decisions"] == ["accept", "cancel"]
    assert "curl" in pending["summary"]


def test_the_wire_shape_has_exactly_one_definition(manager):
    """`as_dict` says "one definition, because two would drift". Hold it to that."""
    from codex_pilot.snapshot import parse_pending

    (request,) = parse_pending([REAL_COMMAND_REQUEST])
    manager.handle_frame(snapshot_frame([REAL_COMMAND_REQUEST]))
    (via_health,) = manager.health_of(THREAD)["pending"]
    assert via_health == request.as_dict()
