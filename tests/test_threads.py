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


def store(home: Path, holders: dict[str, str] | None = None) -> ThreadStore:
    return ThreadStore(home, lock_holder_probe=lambda paths: holders or {})


# -- name resolution ----------------------------------------------------------


def test_resolves_exact_name(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    assert store(home).resolve("Session A") == A


def test_uuid_passes_through_without_index_lookup(home):
    assert store(home).resolve(A) == A


def test_uuid_is_case_normalised(home):
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


def test_thread_with_a_lock_holder_is_app_owned(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    (home / "thread-writer-locks" / f"{A}.lock").touch()
    info = store(home, holders={A: "codex(7687)"}).describe(A)
    assert info.holder == "codex(7687)"
    assert info.app_owned is True


def test_thread_without_a_lock_holder_is_resumable(home):
    write_index(home, [(A, "Session A", "2026-08-26T09:00:00Z")])
    info = store(home).describe(A)
    assert info.holder is None
    assert info.app_owned is False


def test_stale_lock_file_with_no_holder_is_not_owned(home):
    # A lock file can outlive the process; only an open fd counts.
    (home / "thread-writer-locks" / f"{A}.lock").touch()
    assert store(home, holders={}).describe(A).app_owned is False


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


def test_list_open_returns_only_app_owned_threads(home):
    write_index(
        home,
        [
            (A, "Session A", "2026-08-26T09:00:00Z"),
            (B, "Session B", "2026-08-26T09:00:01Z"),
        ],
    )
    for tid in (A, B):
        (home / "thread-writer-locks" / f"{tid}.lock").touch()
    listing = store(home, holders={A: "codex(1)"}).list_open()
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
