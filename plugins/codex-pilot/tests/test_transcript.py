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
