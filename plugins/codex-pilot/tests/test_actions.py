"""Session-level behaviour around detached runs.

The writer lock is the thing being protected here. Codex allows one writer per
thread, and since `start_thread` a lock holder can be one of our own `codex exec`
children rather than the app -- so every verb that assumes "lock held means the
app has it" needs to be told otherwise. These tests pin that, and the reporting
that lets an orchestrator notice a detached run finished.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

import pytest

from codex_pilot.actions import ActionError, ResolvedThread, Session
from codex_pilot.follow import EVENT_RUN_FAILED, EVENT_TURN_COMPLETED
from codex_pilot.framing import FrameReader, encode_frame
from codex_pilot.instances import Instance
from codex_pilot.ipc import IpcError
from codex_pilot.resume import DetachedError, DetachedRunner
from codex_pilot.threads import ThreadStore

TID = "01a03f10-e3e1-7b30-9dfc-7c659c4d7434"
STARTED = f'{{"type": "thread.started", "thread_id": "{TID}"}}'


def stub(tmp_path: Path, lines: list[str], sleep: float = 0.0, exit_code: int = 0) -> Path:
    """A fake `codex` that emits `lines`, optionally lingers, then exits."""
    path = tmp_path / f"codex-stub-{abs(hash((tuple(lines), sleep, exit_code))) % 10**8}"
    emit = "\n".join(f"printf '%s\\n' '{line}'" for line in lines)
    path.write_text(f"#!/bin/sh\n{emit}\nsleep {sleep}\nexit {exit_code}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "codexhome"
    (h / "thread-writer-locks").mkdir(parents=True)
    (h / "sessions" / "2026" / "08" / "26").mkdir(parents=True)
    (h / "archived_sessions").mkdir(parents=True)
    return h


def write_rollout(home: Path, thread_id: str, cwd: Path) -> None:
    sub = home / "sessions" / "2026" / "08" / "26"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"rollout-2026-08-26T10-00-00-{thread_id}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": str(cwd), "id": thread_id}}) + "\n"
    )


def session(home: Path, tmp_path: Path, binary: Path, holders: dict[str, str] | None = None):
    inst = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
    sess = Session(instances=[inst])
    store = ThreadStore(home, lock_holder_probe=lambda paths: dict(holders or {}))
    sess._stores["default"] = store
    sess._runners["default"] = DetachedRunner(inst, store, codex_binary=binary)
    return sess, inst


# -- the lock guard -----------------------------------------------------------


def test_send_message_refuses_while_our_own_run_holds_the_lock(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED], sleep=5))
    sess.start_thread("build it", cwd=str(work))
    write_rollout(home, TID, work)
    # Two writers on one rollout corrupt it; the guard is the only thing
    # standing between an orchestrator and that.
    with pytest.raises(ActionError, match="still holds the writer lock"):
        sess.send_message(TID, "and another thing")
    sess.close()


def test_steer_and_focus_refuse_while_our_own_run_holds_the_lock(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED], sleep=5))
    sess.start_thread("build it", cwd=str(work))
    write_rollout(home, TID, work)
    # focus_thread is the dangerous one: it would ask the app to open a thread
    # our own writer still holds, which is exactly what the lock prevents.
    for call in (lambda: sess.steer_turn(TID, "x"), lambda: sess.focus_thread(TID)):
        with pytest.raises(ActionError, match="still holds the writer lock"):
            call()
    sess.close()


def test_the_refusal_lifts_once_the_run_finishes(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED]))
    out = sess.start_thread("build it", cwd=str(work))
    write_rollout(home, TID, work)
    sess._runs[TID].wait(timeout=15)
    assert sess.live_run(TID) is None
    # No longer ours, so focus_thread is allowed again.
    assert sess.focus_thread(TID)["thread"] == out["thread"]
    sess.close()


def test_send_message_registers_its_own_detached_run(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, TID, work)
    sess, _ = session(home, tmp_path, stub(tmp_path, ["resumed"], sleep=5))
    sess.send_message(TID, "go")
    # A resume takes the lock the same way a start does; if it is not tracked,
    # every other verb misreads our lock as the app's.
    assert sess.live_run(TID) is not None
    with pytest.raises(ActionError, match="still holds the writer lock"):
        sess.send_message(TID, "again")
    sess.close()


def test_a_new_run_replaces_a_finished_one_for_the_same_thread(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, TID, work)
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED]))
    sess.start_thread("first", cwd=str(work))
    first = sess._runs[TID]
    first.wait(timeout=15)
    sess._runners["default"] = DetachedRunner(
        sess.instances[0],
        sess.store(sess.instances[0]),
        codex_binary=stub(tmp_path, ["x"], sleep=5),
    )
    sess.send_message(TID, "second")
    # A stale entry would report `running: False` while a different process is
    # actively writing the thread.
    assert sess._runs[TID].pid != first.pid
    assert sess.live_run(TID) is not None
    sess.close()


# -- route reporting ----------------------------------------------------------


def test_route_says_running_when_the_lock_is_ours_not_the_apps(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED], sleep=5), holders={TID: "codex(1)"})
    sess.start_thread("build it", cwd=str(work))
    write_rollout(home, TID, work)
    row = next(r for r in sess.list_threads() if r["thread"] == TID)
    # The lock is held, but by us. "desktop" would send callers down an IPC
    # route that cannot work; plain "detached" would promise it is free to
    # resume, which it is not.
    assert row["route"] == "detached_running"
    assert row["started_here"] is True
    assert row["running"] is True
    sess.close()


def test_a_thread_we_started_stays_listed_once_it_goes_idle(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED]))
    sess.start_thread("build it", cwd=str(work))
    write_rollout(home, TID, work)
    sess._runs[TID].wait(timeout=15)
    # It holds no lock now, so nothing else would surface it.
    rows = sess.list_threads()
    assert [r["thread"] for r in rows] == [TID]
    # And it really is free to resume again.
    assert rows[0]["route"] == "detached"
    assert rows[0]["running"] is False
    sess.close()


# -- completion reaches collect_events ---------------------------------------


def test_a_finished_detached_run_reports_turn_completed(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, inst = session(home, tmp_path, stub(tmp_path, [STARTED]))
    sess.start_thread("build it", cwd=str(work))
    sess._runs[TID].wait(timeout=15)
    sess._reap_runs(inst)
    got = sess.collect_events()
    # Without this a fan-out would wait forever for the threads it started:
    # a detached run is not mounted in the app, so it broadcasts nothing.
    assert [(e["thread"], e["type"]) for e in got["events"]] == [(TID, EVENT_TURN_COMPLETED)]
    assert got["events"][0]["data"]["route"] == "detached"
    sess.close()


def test_a_failed_detached_run_is_reported_as_failed(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, inst = session(home, tmp_path, stub(tmp_path, [STARTED], exit_code=3))
    sess.start_thread("build it", cwd=str(work))
    sess._runs[TID].wait(timeout=15)
    sess._reap_runs(inst)
    events = sess.collect_events()["events"]
    assert [e["type"] for e in events] == [EVENT_RUN_FAILED]
    assert events[0]["data"]["returncode"] == 3
    sess.close()


def test_a_run_is_reported_only_once(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, inst = session(home, tmp_path, stub(tmp_path, [STARTED]))
    sess.start_thread("build it", cwd=str(work))
    sess._runs[TID].wait(timeout=15)
    for _ in range(3):
        sess._reap_runs(inst)
    assert len(sess.collect_events()["events"]) == 1
    sess.close()


def test_reporting_a_detached_run_does_not_make_it_a_follow(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, inst = session(home, tmp_path, stub(tmp_path, [STARTED]))
    sess.start_thread("build it", cwd=str(work))
    sess._runs[TID].wait(timeout=15)
    sess._reap_runs(inst)
    # It has no stream, so it must not be resynced or reported as lost.
    assert sess.follow_manager(inst).followed == []
    assert sess.collect_events()["following"] == []
    sess.close()


# -- argument validation ------------------------------------------------------


def test_start_thread_refuses_an_unsandboxed_run(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED]))
    with pytest.raises(DetachedError, match="danger-full-access"):
        sess.start_thread("x", cwd=str(work), sandbox="danger-full-access")
    sess.close()


def test_start_thread_refuses_an_unknown_approval_policy(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED]))
    with pytest.raises(DetachedError, match="approval must be one of"):
        sess.start_thread("x", cwd=str(work), approval="yolo")
    sess.close()


# -- stopping our own run -----------------------------------------------------


def test_stop_turn_terminates_a_run_we_started(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, _ = session(home, tmp_path, stub(tmp_path, [STARTED], sleep=30))
    sess.start_thread("build it", cwd=str(work))
    write_rollout(home, TID, work)
    assert sess.live_run(TID) is not None
    out = sess.stop_turn(TID)
    # start_thread launches an unattended agent; without this there is no way
    # to stop one from here at all, because it is not in the app to interrupt.
    assert out["stopped"] is True
    assert out["route"] == "detached"
    assert sess.live_run(TID) is None
    sess.close()


def test_stopping_kills_the_whole_process_group(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "child-still-alive"
    path = tmp_path / "codex-spawner"
    path.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{STARTED}'\n( sleep 20; touch '{marker}' ) &\nsleep 20\n"
    )
    path.chmod(0o755)
    sess, _ = session(home, tmp_path, path)
    sess.start_thread("build it", cwd=str(work))
    write_rollout(home, TID, work)
    sess.stop_turn(TID)
    # The agent's own children hold the workspace too; leaving them behind
    # would leave work running that nothing is tracking.
    assert not marker.exists()
    sess.close()


def test_a_late_thread_id_is_picked_up_rather_than_lost(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    inst_stub = tmp_path / "codex-slow"
    inst_stub.write_text(f"#!/bin/sh\nsleep 0.6\nprintf '%s\\n' '{STARTED}'\nsleep 5\n")
    inst_stub.chmod(0o755)
    sess, inst = session(home, tmp_path, inst_stub)
    sess._runners["default"].start = partial(sess._runners["default"].start, wait_for_id=0.1)
    out = sess.start_thread("build it", cwd=str(work))
    # Gave up waiting, but the run is alive and the id is still coming.
    assert out["thread"] is None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and sess.live_run(TID) is None:
        sess._adopt_late_ids(inst)
        time.sleep(0.1)
    # Otherwise the run stays untracked for good: its lock reads as the app's,
    # nothing reports its completion, and the child is never reaped.
    assert sess.live_run(TID) is not None
    write_rollout(home, TID, work)
    sess.stop_turn(TID)
    sess.close()


def test_a_run_we_stopped_is_not_reported_as_having_failed(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sess, inst = session(home, tmp_path, stub(tmp_path, [STARTED], sleep=30))
    sess.start_thread("build it", cwd=str(work))
    write_rollout(home, TID, work)
    sess.stop_turn(TID)
    sess._reap_runs(inst)
    event = sess.collect_events()["events"][0]
    # It exited non-zero because we killed it, which is not the agent failing.
    assert event["data"]["stopped"] is True
    sess.close()


# -- connection health and reconnect ------------------------------------------
#
# Nothing here had test coverage before: `Session.client`'s cache-validity check,
# the listener re-attach, and the pump were all exercised only against a live
# app. The morning's wedge happened inside exactly that gap.


class FakeApp:
    """A unix socket that answers `initialize`, and can re-bind like a restart."""

    def __init__(self) -> None:
        # macOS caps AF_UNIX paths at 104 bytes, and pytest's tmp_path is already
        # most of that, so this CODEX_HOME lives in a short temp dir of its own.
        # A symlink does not help: connect() measures the literal path it is given.
        self.home = Path(tempfile.mkdtemp(prefix="cp"))
        (self.home / "ipc").mkdir()
        (self.home / "thread-writer-locks").mkdir()
        (self.home / "sessions").mkdir()
        (self.home / "archived_sessions").mkdir()
        self.path = self.home / "ipc" / "ipc.sock"
        self.connections: list[socket.socket] = []
        self.frames: list[dict] = []
        self.answer_initialize = True
        self._stop = threading.Event()
        self._srv: socket.socket | None = None
        self.bind()

    def bind(self) -> None:
        """(Re)create the listening socket, as a restarting app would."""
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.path))
        srv.listen(8)
        self._srv = srv
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def rebind(self) -> None:
        self.close_clients()
        if self._srv is not None:
            with contextlib.suppress(OSError):
                self._srv.close()
        self.bind()

    def close_clients(self) -> None:
        for conn in self.connections:
            with contextlib.suppress(OSError):
                conn.close()
        self.connections = []

    def _accept(self, srv: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            self.connections.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        reader = FrameReader()
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            for msg in reader.feed(chunk):
                self.frames.append(msg)
                if msg.get("method") == "initialize" and self.answer_initialize:
                    reply = {
                        "type": "response",
                        "requestId": msg["requestId"],
                        "resultType": "success",
                        "method": "initialize",
                        "handledByClientId": "c1",
                        "result": {"clientId": "c1"},
                    }
                    with contextlib.suppress(OSError):
                        conn.sendall(encode_frame(reply))

    def stop(self) -> None:
        self._stop.set()
        self.close_clients()
        if self._srv is not None:
            with contextlib.suppress(OSError):
                self._srv.close()
        shutil.rmtree(self.home, ignore_errors=True)


@pytest.fixture
def app():
    fake = FakeApp()
    yield fake
    fake.stop()


def live_session(app: FakeApp, ipc_timeout: float = 1.0):
    inst = Instance(slug="default", codex_home=app.home, app_path=None, is_default=True)
    sess = Session(instances=[inst], ipc_timeout=ipc_timeout)
    sess._stores["default"] = ThreadStore(app.home, lock_holder_probe=lambda paths: {})
    return sess, inst


def test_client_is_reused_while_the_socket_is_the_same(app):
    sess, inst = live_session(app)
    try:
        assert sess.client(inst) is sess.client(inst)
    finally:
        sess.close()


def test_a_rebound_socket_forces_a_fresh_handshake(app):
    """The path survives a restart; the inode does not. That is the only tell."""
    sess, inst = live_session(app)
    try:
        first = sess.client(inst)
        first_identity = first.socket_identity
        app.rebind()

        second = sess.client(inst)
        assert second is not first
        assert second.client_id == "c1"
        assert second.socket_identity != first_identity
        assert first.is_closed
    finally:
        sess.close()


def test_a_failed_handshake_leaves_nothing_behind(app):
    """A wedged app still accepts the connection, then never answers.

    Without an explicit close the reader thread blocks on recv forever, and the
    pump retrying every few seconds would leak one per attempt for the whole
    outage.
    """
    sess, inst = live_session(app, ipc_timeout=0.4)
    try:
        app.answer_initialize = False
        before = threading.active_count()
        for _ in range(3):
            with pytest.raises(IpcError):
                sess.client(inst)
        assert "default" not in sess._clients
        # Reader threads are asked to stop, not stopped synchronously, so give
        # them a moment rather than racing their exit.
        deadline = time.monotonic() + 3.0
        while threading.active_count() > before and time.monotonic() < deadline:
            time.sleep(0.02)
        assert threading.active_count() <= before
    finally:
        sess.close()


# Reconnect re-subscription itself lives in tests/test_reconnect.py, which
# drives it against the shared fake app. What is kept here is the half-open
# case that harness cannot express: a re-bind with the old connection still
# open, where `is_closed` never trips and only the inode tells you.


def test_a_resubscribe_that_fails_to_send_is_not_dropped(app):
    """take_resync_requests empties the queue, so a failed broadcast loses it."""
    sess, inst = live_session(app)
    try:
        manager = sess.follow_manager(inst)
        manager.follow(TID)
        manager.resync_all("reconnected")

        taken = manager.take_resync_requests()
        assert taken == [TID]
        assert manager.take_resync_requests() == []

        manager.requeue_resync(taken)
        assert manager.take_resync_requests() == [TID]
    finally:
        sess.close()


def test_requeue_ignores_threads_that_stopped_being_followed(app):
    sess, inst = live_session(app)
    try:
        manager = sess.follow_manager(inst)
        manager.requeue_resync(["gone-thread"])
        assert manager.take_resync_requests() == []
    finally:
        sess.close()


# -- cursor epoch -------------------------------------------------------------
#
# Sequence numbers restart at 0 when the MCP server process does, and `collect`
# only returns events with `seq > after`. A supervisor that kept its cursor
# across a bounce therefore filters out every event that follows, forever, with
# nothing in the response to say so.


def test_collect_events_reports_an_epoch(app):
    sess, _ = live_session(app)
    try:
        got = sess.collect_events()
        assert got["epoch"]
        assert got["cursor_reset"] is False
    finally:
        sess.close()


def test_a_cursor_from_a_previous_process_is_reset_not_obeyed(app):
    sess, inst = live_session(app)
    try:
        manager = sess.follow_manager(inst)
        manager.emit_external(TID, EVENT_TURN_COMPLETED, {"route": "detached"})

        stale = sess.collect_events(after=9999, epoch="a-previous-process")
        assert stale["cursor_reset"] is True
        assert [e["thread"] for e in stale["events"]] == [TID]
    finally:
        sess.close()


def test_a_cursor_beyond_the_current_sequence_is_reset(app):
    """Even with no epoch passed, an impossible cursor is a bounce tell."""
    sess, inst = live_session(app)
    try:
        manager = sess.follow_manager(inst)
        manager.emit_external(TID, EVENT_TURN_COMPLETED, {"route": "detached"})

        got = sess.collect_events(after=9999)
        assert got["cursor_reset"] is True
        assert len(got["events"]) == 1
    finally:
        sess.close()


def test_a_matching_epoch_honours_the_cursor(app):
    sess, inst = live_session(app)
    try:
        manager = sess.follow_manager(inst)
        manager.emit_external(TID, EVENT_TURN_COMPLETED, {"route": "detached"})
        first = sess.collect_events()
        assert len(first["events"]) == 1

        again = sess.collect_events(after=first["cursor"], epoch=first["epoch"])
        assert again["cursor_reset"] is False
        assert again["events"] == []
    finally:
        sess.close()


def test_a_lost_follow_still_resubscribes_when_state_is_read(app):
    """Registered is not the same as healthy.

    `snapshot` skipped the subscribe whenever a follow was registered, to avoid
    unsubscribing a live one. But a follow that lost its connection is
    registered and receiving nothing, so that skip meant waiting the full
    timeout for a frame that could not arrive -- and reporting "could not read
    stream state" on a thread the app would have answered for.
    """
    sess, inst = live_session(app)
    try:
        sess.client(inst)
        manager = sess.follow_manager(inst)
        manager.follow(TID)
        manager.lost(TID, "IPC connection closed")
        before = len(
            [f for f in app.frames if f.get("method") == "thread-stream-following-changed"]
        )

        resolved = ResolvedThread(instance=inst, thread_id=TID, info=sess.store(inst).describe(TID))
        sess.snapshot(resolved, wait=0.2)

        follows = [f for f in app.frames if f.get("method") == "thread-stream-following-changed"]
        assert len(follows) > before, "an unhealthy follow was never re-subscribed"
        # The registration survives, so it must not be torn down on the way out.
        assert all(f["params"]["following"] is True for f in follows[before:])
        assert manager.is_following(TID)
    finally:
        sess.close()
