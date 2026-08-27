"""Reading a thread's transcript off disk.

This is the tier that always works. Stream state only reaches a thread the app
has mounted, and it holds a bounded set -- of 13 lock-holding threads on a real
instance, 4 answered -- so for most threads there is no live state to read at
all. The rollout is not subject to any of that: it is on disk for every thread,
mounted or not, running or idle, and it is what the thread actually said.

Rollouts reach tens of megabytes, almost all of it reasoning traces and
token-count events. So this reads the tail and keeps the record types that carry
meaning: what was asked, what was answered, and which tools ran. Reasoning is
available but off by default, because including it is usually how a harvest
turns back into a context problem.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Enough tail for a long turn, small enough not to read a whole rollout.
TAIL_BYTES = 2_000_000
DEFAULT_LIMIT = 40

# A turn opens with `task_started` and closes with either of the other two.
# Deliberately not a closed vocabulary check: the record types differ between
# Codex instances, so anything unrecognised is skipped rather than assumed.
TURN_OPEN = "task_started"
TURN_CLOSE = frozenset({"task_complete", "turn_aborted"})
# Boundaries are sparse compared to reasoning and token-count records, so the
# tail has to be generous enough to hold one for a long turn.
PHASE_TAIL_BYTES = 4_000_000

ROLE_RECORDS = {"message", "agent_message"}
CALL_RECORDS = {"function_call", "custom_tool_call", "local_shell_call"}
OUTPUT_RECORDS = {"function_call_output", "custom_tool_call_output"}


def _text_of(payload: dict[str, Any]) -> str:
    """Pull readable text out of the several shapes a message can take."""
    direct = payload.get("text")
    if isinstance(direct, str):
        return direct
    content = payload.get("content")
    if isinstance(content, list):
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "\n".join(p for p in parts if p)
    if isinstance(content, str):
        return content
    return ""


def _tail_lines(path: Path, tail_bytes: int) -> list[bytes]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - tail_bytes))
        data = fh.read()
    lines = data.splitlines()
    # A seek into the middle of the file usually lands mid-line; that partial
    # first line is not a record.
    if size > tail_bytes and lines:
        lines = lines[1:]
    return lines


def read(
    rollout: Path,
    limit: int = DEFAULT_LIMIT,
    include_reasoning: bool = False,
    tail_bytes: int = TAIL_BYTES,
) -> dict[str, Any]:
    """The last `limit` meaningful entries of a rollout, newest last."""
    try:
        lines = _tail_lines(rollout, tail_bytes)
    except OSError as exc:
        return {"error": f"could not read {rollout}: {exc}", "entries": []}

    entries: list[dict[str, Any]] = []
    cwd: str | None = None
    turn_id: str | None = None
    for raw in lines:
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = record.get("type")
        stamp = record.get("timestamp")

        if kind == "turn_context":
            # Latest wins: a thread's cwd can be changed between turns.
            cwd = payload.get("cwd") or cwd
            turn_id = payload.get("turn_id") or turn_id
            continue
        if kind != "response_item":
            continue

        ptype = payload.get("type")
        if ptype == "reasoning":
            if not include_reasoning:
                continue
            summary = payload.get("summary")
            text = ""
            if isinstance(summary, list):
                text = "\n".join(s.get("text", "") for s in summary if isinstance(s, dict)).strip()
            entries.append({"at": stamp, "kind": "reasoning", "text": text})
        elif ptype in ROLE_RECORDS:
            entries.append(
                {
                    "at": stamp,
                    "kind": "message",
                    "role": payload.get("role") or "assistant",
                    "author": payload.get("author"),
                    "text": _text_of(payload),
                }
            )
        elif ptype in CALL_RECORDS:
            entries.append(
                {
                    "at": stamp,
                    "kind": "tool_call",
                    "name": payload.get("name"),
                    "namespace": payload.get("namespace"),
                    "call_id": payload.get("call_id"),
                }
            )
        elif ptype in OUTPUT_RECORDS:
            output = payload.get("output")
            entries.append(
                {
                    "at": stamp,
                    "kind": "tool_output",
                    "call_id": payload.get("call_id"),
                    "text": output if isinstance(output, str) else json.dumps(output)[:4000],
                }
            )

    truncated = len(entries) > limit
    return {
        "rollout": str(rollout),
        "cwd": cwd,
        "turn_id": turn_id,
        "entries": entries[-limit:] if limit > 0 else entries,
        "truncated": truncated,
    }


# A timestamp is ISO-8601 and nothing else. The value is file content, and the
# `disk` block puts it in front of a model, so it is bounded and shape-checked
# rather than passed through: a rollout is writable by anything with access to
# CODEX_HOME, and an unbounded string here would be a free channel into an
# orchestrator's context.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _timestamp_of(record: dict[str, Any]) -> str | None:
    at = record.get("timestamp")
    if not isinstance(at, str) or not _TIMESTAMP_RE.match(at):
        return None
    return at


@dataclass(frozen=True)
class RolloutPhase:
    """Whether a thread's rollout ends inside a turn or between turns."""

    phase: str
    last_boundary: str
    last_boundary_at: str | None

    @property
    def mid_turn(self) -> bool:
        return self.phase == "mid_turn"

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "last_boundary": self.last_boundary,
            "last_boundary_at": self.last_boundary_at,
        }


def rollout_turn_phase(rollout: Path, tail_bytes: int = PHASE_TAIL_BYTES) -> RolloutPhase | None:
    """Whether the rollout's last turn boundary opened a turn or closed one.

    This exists because stream state is unreadable for a thread the app holds
    but does not render, and `state: None` on its own cannot be told apart from
    "nothing pending". It does not recover pending approvals -- rollouts contain
    no record for one, enumerated across 1,096 real files -- but "abandoned
    mid-turn twenty minutes ago" and "idle" are very different things to a
    supervisor, and both are on disk.

    The *last* boundary in file order decides it, never a count of opens against
    closes: rollouts routinely open with an out-of-order burst, including a
    `task_complete` written before a `task_started` at the same millisecond, and
    counting reads those as permanently wedged.

    None when the tail holds no boundary at all, which is "unknown" and must not
    be reported as either phase.
    """
    try:
        lines = _tail_lines(rollout, tail_bytes)
    except OSError:
        return None

    boundary: str | None = None
    stamp: str | None = None
    for raw in lines:
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            # A rollout being written to has a torn last line; that is normal.
            continue
        except RecursionError:
            # Deeply nested JSON. Not something the app writes, but a rollout is
            # just a file: anything with write access to CODEX_HOME can plant one,
            # and `_find_rollout` takes the first sorted match. A crash here would
            # take out thread_status, which is what a supervisor calls when it
            # already cannot see.
            continue
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        if ptype == TURN_OPEN or ptype in TURN_CLOSE:
            boundary = str(ptype)
            stamp = _timestamp_of(record)

    if boundary is None:
        return None
    return RolloutPhase(
        phase="mid_turn" if boundary == TURN_OPEN else "idle",
        last_boundary=boundary,
        last_boundary_at=stamp,
    )
