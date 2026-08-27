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
