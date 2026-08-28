"""Thread discovery: name resolution, writer locks, rollout inspection.

Everything here is scoped to a CODEX_HOME, because that is what separates two
Codex Desktop instances (Doppel stamps LSEnvironment.CODEX_HOME per app bundle).
Thread ids are unique per instance, not globally.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_pilot.threads import AmbiguousThreadError, ThreadStore, UnknownThreadError

A = "01a039f5-2b49-7ef3-9b43-92673a44dd43"
B = "01a039f5-d257-7f92-80c6-315b959dec95"

# Two lock holders that lsof cannot tell apart: both report the command name
# "codex". Only the pid separates the app-server the app runs from a
# `codex exec resume` child, which is the whole point of classifying by pid.
APP_PID = 78222
EXEC_PID = 69843


def write_index(home: Path, rows: list[tuple[str, str, str]]) -> None:
    with (home / "session_index.jsonl").open("w") as fh:
        for tid, name, ts in rows:
            fh.write(json.dumps({"id": tid, "thread_name": name, "updated_at": ts}) + "\n")


def write_rollout(home: Path, tid: str, cwd: str, turn_id: str, archived: bool = False) -> Path:
    sub = home / ("archived_sessions" if archived else "sessions/2026/08/26")
    sub.mkdir(parents=True, exist_ok=True)
    path = sub / f"rollout-2026-08-26T10-00-00-{tid}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"cwd": cwd, "id": tid}},
        {"timestamp": "2026-08-26T11:00:00.000Z", "type": "event_msg", "payload": {"type": "x"}},
        {
            "timestamp": "2026-08-26T11:00:05.000Z",
            "type": "event_msg",
            "payload": {"type": "item_completed", "turn_id": turn_id},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "thread-writer-locks").mkdir()
    (tmp_path / "sessions").mkdir()
    (tmp_path / "archived_sessions").mkdir()
    return tmp_path


def store(
    home: Path,
    holders: dict[str, tuple[int, str]] | None = None,
    app_pids: frozenset[int] | None = frozenset({APP_PID}),
) -> ThreadStore:
    return ThreadStore(
        home,
        lock_holder_probe=lambda paths: dict(holders or {}),
        app_process_probe=lambda socks: None if app_pids is None else set(app_pids),
    )


# -- name resolution ----------------------------------------------------------


def test_resolves_exact_name(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    assert store(home).resolve("Session A") == A


def test_uuid_resolves_when_the_instance_holds_it(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    assert store(home).resolve(A) == A


def test_uuid_is_case_normalised(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    assert store(home).resolve(A.upper()) == A


def test_newest_name_wins_after_a_rename(home):
    # The index is append-only; a renamed thread has several rows.
    write_index(
        home,
        [
            (A, "Experiment with ideas", "2026-08-26T09:00:00Z"),
            (A, "ABC", "2026-08-26T09:30:00Z"),
        ],
    )
    s = store(home)
    assert s.resolve("ABC") == A
    assert s.display_name(A) == "ABC"


def test_old_name_still_resolves_to_the_same_thread(home):
    write_index(
        home,
        [
            (A, "Experiment with ideas", "2026-08-26T09:00:00Z"),
            (A, "ABC", "2026-08-26T09:30:00Z"),
        ],
    )
    assert store(home).resolve("Experiment with ideas") == A


def test_substring_match_when_unique(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    assert store(home).resolve("ession A") == A


def test_ambiguous_substring_raises_and_lists_candidates(home):
    write_index(
        home,
        [
            (A, "Session A", "2026-08-26T09:00:00Z"),
            (B, "Session B", "2026-08-26T09:00:01Z"),
        ],
    )
    with pytest.raises(AmbiguousThreadError) as exc:
        store(home).resolve("Session")
    assert "Session A" in str(exc.value)
    assert "Session B" in str(exc.value)


def test_unknown_name_raises(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    with pytest.raises(UnknownThreadError):
        store(home).resolve("Nothing Like This")


def test_exact_match_beats_substring(home):
    write_index(
        home,
        [
            (A, "Session", "2026-08-26T09:00:00Z"),
            (B, "Session B", "2026-08-26T09:00:01Z"),
        ],
    )
    assert store(home).resolve("Session") == A


def test_missing_index_file_is_not_an_error(home):
    (home / "session_index.jsonl").unlink(missing_ok=True)
    assert store(home).names() == {}


def test_corrupt_index_lines_are_skipped(home):
    (home / "session_index.jsonl").write_text(
        '{"id":"' + A + '","thread_name":"Session A","updated_at":"2026-08-26T09:00:00Z"}\n'
        "not json at all\n"
    )
    assert store(home).resolve("Session A") == A


# -- writer locks -------------------------------------------------------------


def test_lock_held_by_the_app_is_app_owned(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    (home / "thread-writer-locks" / f"{A}.lock").touch()
    info = store(home, holders={A: (APP_PID, "codex")}).describe(A)
    assert str(info.holder) == f"codex({APP_PID})"
    assert info.app_owned is True
    assert info.holder is not None
    assert info.resumable is False


def test_thread_without_a_lock_holder_is_resumable(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    info = store(home).describe(A)
    assert info.holder is None
    assert info.app_owned is False
    assert info.holder is None
    assert info.resumable is True


def test_stale_lock_file_with_no_holder_is_not_owned(home):
    # A lock file can outlive the process; only an open fd counts.
    (home / "thread-writer-locks" / f"{A}.lock").touch()
    assert store(home, holders={}).describe(A).app_owned is False
    assert store(home, holders={}).describe(A).resumable is True


# -- rollout inspection -------------------------------------------------------


def test_reads_cwd_and_turn_id_from_rollout(home):
    write_rollout(home, A, "/work/tree-a", "turn-99")
    info = store(home).describe(A)
    assert info.cwd == "/work/tree-a"
    assert info.turn_id == "turn-99"


def test_detects_archived_rollout(home):
    write_rollout(home, A, "/work/tree-a", "turn-99", archived=True)
    info = store(home).describe(A)
    assert info.archived is True


def test_active_rollout_is_not_archived(home):
    write_rollout(home, A, "/work/tree-a", "turn-99")
    assert store(home).describe(A).archived is False


def test_missing_rollout_degrades_without_raising(home):
    info = store(home).describe(A)
    assert info.rollout is None
    assert info.cwd is None
    assert info.turn_id is None


def test_last_event_timestamp_is_parsed(home):
    write_rollout(home, A, "/w", "turn-1")
    assert store(home).describe(A).last_event is not None


# -- listing ------------------------------------------------------------------


def test_list_open_returns_only_lock_holding_threads(home):
    write_index(
        home,
        [
            (A, "Session A", "2026-08-26T09:00:00Z"),
            (B, "Session B", "2026-08-26T09:00:01Z"),
        ],
    )
    for tid in (A, B):
        (home / "thread-writer-locks" / f"{tid}.lock").touch()
    listing = store(home, holders={A: (APP_PID, "codex")}).list_open()
    assert [i.thread_id for i in listing] == [A]


# -- instance scoping ---------------------------------------------------------


def test_two_codex_homes_are_independent(tmp_path):
    for n in ("primary", "secondary"):
        (tmp_path / n / "thread-writer-locks").mkdir(parents=True)
    write_index(tmp_path / "primary", [(A, "Session A", "2026-08-26T09:00:00Z")])
    write_index(tmp_path / "secondary", [(B, "Session A", "2026-08-26T09:00:00Z")])
    # Same display name, different instance, different thread.
    assert store(tmp_path / "primary").resolve("Session A") == A
    assert store(tmp_path / "secondary").resolve("Session A") == B


# -- existence ----------------------------------------------------------------


def test_known_thread_exists(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    assert store(home).exists(A) is True


def test_thread_with_only_a_rollout_exists(home):
    # A CLI-created thread may not be in the session index yet.
    write_rollout(home, A, "/w", "t1")
    assert store(home).exists(A) is True


def test_archived_thread_exists(home):
    write_rollout(home, A, "/w", "t1", archived=True)
    assert store(home).exists(A) is True


def test_unknown_id_does_not_exist(home):
    assert store(home).exists(B) is False


def test_resolve_rejects_a_uuid_that_is_not_in_this_instance(home):
    # Without this, a bare id "resolves" in every instance, so the caller cannot
    # tell which one owns it -- and with one instance live it would bind to the
    # wrong store entirely.
    with pytest.raises(UnknownThreadError):
        store(home).resolve(B)


def test_resolve_accepts_a_uuid_present_in_this_instance(home):
    write_rollout(home, A, "/w", "t1")
    assert store(home).resolve(A) == A


# -- lock holder classification -----------------------------------------------
#
# lsof names both the app's `codex app-server` child and a detached
# `codex exec resume` "codex", so the command name cannot separate them. What
# can is the pid: the app's writer is the process serving the instance's IPC
# socket, or a descendant of it. Everything else holding a lock is some other
# writer, and neither route reaches a thread it holds.


def test_lock_held_by_a_foreign_process_is_not_app_owned(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    info = store(home, holders={A: (EXEC_PID, "codex")}).describe(A)
    assert info.holder is not None
    assert info.holder.is_app is False
    assert info.app_owned is False
    # Locked all the same, so it must never be offered to the detached route.
    assert info.holder is not None
    assert info.resumable is False


def test_app_descendants_count_as_the_app(home):
    # The lock is held by the `codex app-server` child, not by the process that
    # actually listens on the socket -- verified live: pid 78315 (app-server)
    # held four locks, and its parent 78222 (ChatGPT) owned the socket.
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    app = store(home, holders={A: (78315, "codex")}, app_pids=frozenset({78222, 78315}))
    info = app.describe(A)
    assert info.app_owned is True


def test_unclassifiable_holder_is_neither_owned_nor_resumable(home):
    # The app-pid probe could not run, so who holds the lock is unknown. That
    # is not "the app" and it is certainly not "free".
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    info = store(home, holders={A: (EXEC_PID, "codex")}, app_pids=None).describe(A)
    assert info.holder is not None
    assert info.holder.is_app is None
    assert info.app_owned is False
    assert info.resumable is False


def test_lock_probe_failure_is_not_reported_as_an_unlocked_thread(home):
    # A probe that could not run says nothing about the locks. Treating its
    # silence as "no holder" would hand every thread to `codex exec resume`
    # and put a second writer on a rollout the app is already writing.
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    failing = ThreadStore(
        home,
        lock_holder_probe=lambda paths: None,
        app_process_probe=lambda socks: set(),
    )
    info = failing.describe(A)
    assert info.lock_known is False
    assert info.holder is None
    assert info.resumable is False
    assert info.app_owned is False


def test_no_app_serving_the_socket_makes_every_holder_foreign(home):
    # The app is not running, so anything holding a lock is another writer.
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    info = store(home, holders={A: (EXEC_PID, "codex")}, app_pids=frozenset()).describe(A)
    assert info.holder is not None
    assert info.holder.is_app is False


# -- the probes themselves ----------------------------------------------------


def test_lsof_output_is_parsed_into_pid_and_command(monkeypatch):
    # lsof -F emits one `p`/`c` pair per process followed by an `f`/`n` pair
    # per open file, so a process holding several locks appears once.
    from codex_pilot import threads as mod

    monkeypatch.setattr(
        mod,
        "_run_lsof",
        lambda paths: (
            "p78315\nccodex\nf43\nn/l/thread-writer-locks/aaa.lock\n"
            "f44\nn/l/thread-writer-locks/bbb.lock\n"
            "p69843\nccodex\nf7\nn/l/thread-writer-locks/ccc.lock\n"
        ),
    )
    assert mod._lsof_locks([Path("/l")]) == {
        "aaa": (78315, "codex"),
        "bbb": (78315, "codex"),
        "ccc": (69843, "codex"),
    }


def test_an_lsof_that_could_not_run_is_none_not_empty(monkeypatch):
    from codex_pilot import threads as mod

    monkeypatch.setattr(mod, "_run_lsof", lambda paths: None)
    assert mod._lsof_locks([Path("/l")]) is None


def test_subtree_reaches_grandchildren_but_not_siblings():
    from codex_pilot.threads import _subtree

    # 1 -> 100 -> 200 -> 300, and an unrelated 1 -> 400.
    parents = {100: 1, 200: 100, 300: 200, 400: 1}
    assert _subtree({100}, parents) == {100, 200, 300}


def test_no_live_socket_means_no_app_rather_than_unknown(tmp_path):
    from codex_pilot.threads import _app_processes

    assert _app_processes([tmp_path / "ipc.sock"]) == set()


def test_a_listening_socket_is_traced_back_to_its_process():
    # A real AF_UNIX listener, because the whole classification rests on lsof
    # reporting the listening process for a socket path. macOS caps AF_UNIX
    # paths near 104 bytes and pytest's tmp_path already exceeds that, so this
    # binds under a short /tmp directory.
    import os
    import socket as socket_mod
    import tempfile

    from codex_pilot.threads import _app_processes

    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        path = Path(short) / "ipc.sock"
        server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        try:
            server.bind(str(path))
            server.listen(1)
            found = _app_processes([Path(short) / "missing.sock", path])
        finally:
            server.close()
    assert found is not None
    # The listener is this process, and its own children come with it.
    assert os.getpid() in found


def test_a_store_with_no_holders_never_pays_for_the_app_probe(home):
    # The app probe is an lsof plus a full ps sweep. Every `resolve` runs a
    # census, so paying for it when no lock is held at all would tax the
    # common path for nothing.
    calls: list[object] = []
    quiet = ThreadStore(
        home,
        lock_holder_probe=lambda paths: {},
        app_process_probe=lambda socks: calls.append(socks) or set(),
    )
    census = quiet.lock_census()
    assert census.known is True
    assert census.holders == {}
    assert calls == []


# -- aiming the deep link ---------------------------------------------------

STOCK = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
# A Doppel clone renames the executable, so only the bundle component names it.
CLONE = "/Users/x/Applications/ChatGPT Personal.app/Contents/MacOS/ChatGPT.real"


def serving(pids, commands):
    from codex_pilot.threads import serving_app

    return serving_app([Path("/sock")], lambda paths: pids, lambda pid: commands.get(pid))


def test_the_serving_bundle_is_the_one_the_listener_belongs_to():
    assert serving({7}, {7: STOCK}).bundle == Path("/Applications/ChatGPT.app")


def test_a_clone_is_named_by_its_bundle_not_its_renamed_binary():
    # `ChatGPT.real` is not a bundle name and shares none with the stock app;
    # taking the first `.app` component is what keeps the two apart.
    assert serving({7}, {7: CLONE}).bundle == Path("/Users/x/Applications/ChatGPT Personal.app")


def test_nothing_listening_is_an_answer():
    # The app is not running. That is a fact about the app, and the caller can
    # act on it -- unlike a probe that failed.
    found = serving(set(), {})
    assert (found.bundle, found.known) == (None, True)


def test_a_probe_that_could_not_run_is_not_an_answer():
    from codex_pilot.threads import ServingApp, serving_app

    assert serving_app([Path("/sock")], lambda paths: None) == ServingApp.unavailable()


def test_a_listener_ps_cannot_answer_for_is_not_an_answer():
    # The pid was there a moment ago and is not now. Reporting "no app" from
    # that would send the caller to launch an app that is already running.
    assert serving({7}, {}).known is False


def test_a_listener_outside_any_bundle_is_not_an_answer():
    assert serving({7}, {7: "/usr/bin/python3"}).known is False


def test_two_apps_on_one_instance_are_refused_rather_than_picked_between():
    # Two bundles serving one CODEX_HOME is a state we have no answer for, and
    # guessing would aim the link at an app that may not hold the thread.
    assert serving({7, 8}, {7: STOCK, 8: CLONE}).known is False
