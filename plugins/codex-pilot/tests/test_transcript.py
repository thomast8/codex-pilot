"""Reading a thread's transcript off disk.

The disk tier is the one that works for every thread, so it has to survive the
shapes a real rollout actually contains rather than the tidy subset.
"""

from __future__ import annotations

import json
from pathlib import Path

from codex_pilot import transcript


def write(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def item(ptype: str, **payload) -> dict:
    return {
        "timestamp": "2026-08-26T10:00:00.000Z",
        "type": "response_item",
        "payload": {"type": ptype, **payload},
    }


def test_reads_messages_and_tool_calls(tmp_path):
    roll = write(
        tmp_path / "r.jsonl",
        [
            {"type": "turn_context", "payload": {"cwd": "/w", "turn_id": "t1"}},
            item("message", role="user", content=[{"type": "input_text", "text": "do it"}]),
            item("function_call", name="shell", namespace="local", call_id="c1"),
            item("function_call_output", call_id="c1", output="done"),
            item("agent_message", text="finished"),
        ],
    )
    got = transcript.read(roll)
    assert got["cwd"] == "/w"
    assert [e["kind"] for e in got["entries"]] == [
        "message",
        "tool_call",
        "tool_output",
        "message",
    ]
    assert got["entries"][0]["text"] == "do it"
    assert got["entries"][-1]["text"] == "finished"


def test_reasoning_is_excluded_unless_asked_for(tmp_path):
    roll = write(
        tmp_path / "r.jsonl",
        [
            item("reasoning", summary=[{"type": "summary_text", "text": "thinking"}]),
            item("agent_message", text="answer"),
        ],
    )
    # Reasoning dominates a real rollout; including it by default would turn a
    # harvest back into a context problem.
    assert [e["kind"] for e in transcript.read(roll)["entries"]] == ["message"]
    with_reasoning = transcript.read(roll, include_reasoning=True)
    assert [e["kind"] for e in with_reasoning["entries"]] == ["reasoning", "message"]


def test_noise_records_are_skipped(tmp_path):
    roll = write(
        tmp_path / "r.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
            {"type": "world_state", "payload": {"full": True, "state": {}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {}}},
            item("agent_message", text="only this"),
        ],
    )
    entries = transcript.read(roll)["entries"]
    assert len(entries) == 1 and entries[0]["text"] == "only this"


def test_limit_keeps_the_newest(tmp_path):
    roll = write(tmp_path / "r.jsonl", [item("agent_message", text=str(i)) for i in range(10)])
    got = transcript.read(roll, limit=3)
    assert [e["text"] for e in got["entries"]] == ["7", "8", "9"]
    assert got["truncated"] is True


def test_a_partial_first_line_from_the_tail_seek_is_dropped(tmp_path):
    roll = write(tmp_path / "r.jsonl", [item("agent_message", text="x" * 400) for _ in range(20)])
    # Seeking into the middle of a rollout lands mid-line; that fragment is not
    # a record and must not break the read.
    got = transcript.read(roll, tail_bytes=900)
    assert got["entries"]
    assert all(e["kind"] == "message" for e in got["entries"])


def test_malformed_lines_do_not_abort_the_read(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text("{not json\n" + json.dumps(item("agent_message", text="survived")) + "\n[]\n")
    entries = transcript.read(path)["entries"]
    assert [e["text"] for e in entries] == ["survived"]


def test_a_missing_rollout_reports_rather_than_raises(tmp_path):
    got = transcript.read(tmp_path / "gone.jsonl")
    assert got["entries"] == [] and "could not read" in got["error"]


# -- turn phase ---------------------------------------------------------------
#
# The rollout is the only tier that works for a thread the app holds but is not
# rendering. It carries no pending approval -- verified across 1,096 real
# rollouts, there is no record type for one -- but it does carry turn
# boundaries, which separate "abandoned mid-turn" from "idle, nothing to see".


def boundary(ptype: str, at: str = "2026-08-26T10:00:00.000Z") -> dict:
    return {"timestamp": at, "type": "event_msg", "payload": {"type": ptype}}


def test_an_open_turn_reads_as_mid_turn(tmp_path):
    roll = write(
        tmp_path / "r.jsonl",
        [boundary("task_started"), boundary("task_complete"), boundary("task_started")],
    )
    phase = transcript.rollout_turn_phase(roll)
    assert phase is not None
    assert phase.phase == "mid_turn"
    assert phase.last_boundary == "task_started"


def test_a_finished_turn_reads_as_idle(tmp_path):
    roll = write(tmp_path / "r.jsonl", [boundary("task_started"), boundary("task_complete")])
    phase = transcript.rollout_turn_phase(roll)
    assert phase is not None
    assert phase.phase == "idle"


def test_an_aborted_turn_reads_as_idle(tmp_path):
    roll = write(tmp_path / "r.jsonl", [boundary("task_started"), boundary("turn_aborted")])
    phase = transcript.rollout_turn_phase(roll)
    assert phase is not None
    assert phase.phase == "idle"


def test_the_last_boundary_wins_not_the_count(tmp_path):
    """Real rollouts open with an out-of-order burst.

    Observed live: `task_complete` written before `task_started` at an identical
    millisecond, and duplicate `task_started` at the same one. Counting starts
    against completes calls those threads wedged; last-in-file-order does not.
    """
    same = "2026-08-27T10:45:36.837Z"
    roll = write(
        tmp_path / "r.jsonl",
        [
            boundary("task_complete", same),
            boundary("task_started", same),
            boundary("task_started", same),
            boundary("task_complete", "2026-08-27T10:47:00.000Z"),
        ],
    )
    phase = transcript.rollout_turn_phase(roll)
    assert phase is not None
    assert phase.phase == "idle"
    assert phase.last_boundary_at == "2026-08-27T10:47:00.000Z"


def test_a_half_written_last_line_is_tolerated(tmp_path):
    """The app may be mid-write; a torn record is not a reason to give up."""
    roll = tmp_path / "r.jsonl"
    roll.write_text(
        json.dumps(boundary("task_started"))
        + "\n"
        + '{"timestamp": "2026-08-26T10:00:01.000Z", "type": "event_ms'
    )
    phase = transcript.rollout_turn_phase(roll)
    assert phase is not None
    assert phase.phase == "mid_turn"


def test_unknown_event_types_are_ignored(tmp_path):
    """The record vocabulary differs between instances, so it is not a closed set."""
    roll = write(
        tmp_path / "r.jsonl",
        [
            boundary("task_started"),
            boundary("thread_rolled_back"),
            boundary("image_generation_end"),
            boundary("token_count"),
        ],
    )
    phase = transcript.rollout_turn_phase(roll)
    assert phase is not None
    assert phase.phase == "mid_turn"


def test_no_boundary_in_the_window_is_reported_as_unknown(tmp_path):
    """Never guess: a rollout whose tail holds no boundary says nothing."""
    roll = write(tmp_path / "r.jsonl", [item("message", role="user", content="hi")])
    assert transcript.rollout_turn_phase(roll) is None


def test_a_missing_rollout_is_not_an_error(tmp_path):
    assert transcript.rollout_turn_phase(tmp_path / "nope.jsonl") is None
