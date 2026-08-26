"""Thread discovery for one Codex instance.

Everything is scoped to a CODEX_HOME. That is not incidental: Doppel gives each
cloned ChatGPT app its own CODEX_HOME (it stamps `LSEnvironment.CODEX_HOME` into
the bundle), and the socket, session index, writer locks and rollouts all live
under it. Thread ids are unique per instance, so a bare id means nothing without
knowing which CODEX_HOME it came from.

Three facts about a thread matter before acting on it:

- **Who holds its writer lock.** Codex's thread store allows one writer. The
  Desktop app holds a lock on every thread it has open, running or idle, as an
  open fd on `thread-writer-locks/<id>.lock`. An app-owned thread can only be
  driven over IPC; an unowned one can only be driven by `codex exec resume`.
  A lock *file* proves nothing on its own -- it can outlive the process -- so
  ownership is decided by an open fd, via lsof.
- **Its cwd**, read from the rollout, because a resumed turn must run in the
  thread's own worktree rather than wherever the caller happens to be.
- **Its current turn id**, which `interrupt` takes as `expectedTurnId` so a stop
  cannot land on a turn that started after you looked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CWD_RE = re.compile(r'"cwd"\s*:\s*"([^"]+)"')
ROLLOUT_HEAD_LINES = 40
ROLLOUT_TAIL_BYTES = 65536

LockHolderProbe = Callable[[list[Path]], dict[str, str]]


class ThreadError(Exception):
    """Base for thread-resolution failures."""


class UnknownThreadError(ThreadError):
    """No thread matches the given name or id."""


class AmbiguousThreadError(ThreadError):
    """Several threads match; refuse rather than guess which one to drive."""


@dataclass(frozen=True)
class ThreadInfo:
    thread_id: str
    name: str | None
    holder: str | None
    archived: bool
    cwd: str | None
    turn_id: str | None
    last_event: datetime | None
    rollout: Path | None

    @property
    def app_owned(self) -> bool:
        """True when Codex Desktop holds the writer lock (drive it over IPC)."""
        return self.holder is not None

    @property
    def age_seconds(self) -> float | None:
        if self.last_event is None:
            return None
        return (datetime.now(UTC) - self.last_event).total_seconds()


def default_codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def _lsof_probe(paths: list[Path]) -> dict[str, str]:
    """Map thread id -> "command(pid)" for every lock file with an open fd.

    One lsof call for all of them; lsof exits non-zero when nothing matches,
    which is the normal case for an app that is not running.
    """
    if not paths:
        return {}
    try:
        out = subprocess.run(
            ["lsof", "-F", "pcn", *[str(p) for p in paths]],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    held: dict[str, str] = {}
    pid = command = ""
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = value
        elif tag == "c":
            command = value
        elif tag == "n":
            stem = Path(value).stem
            if stem:
                held[stem] = f"{command}({pid})"
    return held


class ThreadStore:
    """Read-only view of one Codex instance's threads."""

    def __init__(
        self, codex_home: Path | None = None, lock_holder_probe: LockHolderProbe | None = None
    ) -> None:
        self.home = codex_home or default_codex_home()
        self._probe = lock_holder_probe or _lsof_probe

    # -- paths --------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.home / "session_index.jsonl"

    @property
    def locks_dir(self) -> Path:
        return self.home / "thread-writer-locks"

    @property
    def socket_path(self) -> Path:
        return self.home / "ipc" / "ipc.sock"

    # -- names --------------------------------------------------------------

    def names(self) -> dict[str, str]:
        """Every name ever assigned, mapped to its thread id.

        The index is append-only, so a renamed thread appears under each of its
        names. Old names keep resolving, which is what you want when a caller
        refers to a thread by a name they saw earlier.
        """
        out: dict[str, str] = {}
        if not self.index_path.exists():
            return out
        with self.index_path.open(errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name, tid = row.get("thread_name"), row.get("id")
                if isinstance(name, str) and isinstance(tid, str):
                    out[name] = tid
        return out

    def display_name(self, thread_id: str) -> str | None:
        """The thread's most recent name, which is what the app shows."""
        latest: tuple[str, str] | None = None
        if not self.index_path.exists():
            return None
        with self.index_path.open(errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("id") != thread_id:
                    continue
                name, ts = row.get("thread_name"), row.get("updated_at") or ""
                if isinstance(name, str) and (latest is None or ts >= latest[1]):
                    latest = (name, ts)
        return latest[0] if latest else None

    def resolve(self, ref: str) -> str:
        """Turn a thread id, exact name, or unique substring into a thread id."""
        if UUID_RE.match(ref.lower()):
            return ref.lower()
        names = self.names()
        if ref in names:
            return names[ref]
        matches = {n: i for n, i in names.items() if ref.lower() in n.lower()}
        distinct = set(matches.values())
        if len(distinct) == 1:
            return next(iter(distinct))
        if not matches:
            raise UnknownThreadError(f"no thread named {ref!r} in {self.home}")
        listing = ", ".join(sorted(matches))
        raise AmbiguousThreadError(f"{ref!r} matches several threads: {listing}")

    # -- locks --------------------------------------------------------------

    def lock_holders(self) -> dict[str, str]:
        if not self.locks_dir.is_dir():
            return {}
        return self._probe(sorted(self.locks_dir.glob("*.lock")))

    # -- rollouts -----------------------------------------------------------

    def _find_rollout(self, thread_id: str) -> tuple[Path | None, bool]:
        """(path, archived). Archiving moves the rollout to archived_sessions/."""
        active = sorted((self.home / "sessions").rglob(f"*{thread_id}*.jsonl"))
        if active:
            return active[0], False
        archived = sorted((self.home / "archived_sessions").rglob(f"*{thread_id}*.jsonl"))
        if archived:
            return archived[0], True
        return None, False

    @staticmethod
    def _read_cwd(path: Path) -> str | None:
        try:
            with path.open(errors="replace") as fh:
                for _ in range(ROLLOUT_HEAD_LINES):
                    line = fh.readline()
                    if not line:
                        break
                    match = CWD_RE.search(line)
                    if match:
                        return match.group(1)
        except OSError:
            return None
        return None

    @staticmethod
    def _read_tail(path: Path) -> dict:
        """Last parseable record. Rollouts reach tens of MB, so read the tail."""
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - ROLLOUT_TAIL_BYTES))
                lines = fh.read().splitlines()
        except OSError:
            return {}
        for raw in reversed(lines):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return {}

    # -- description --------------------------------------------------------

    def describe(self, thread_id: str, holders: dict[str, str] | None = None) -> ThreadInfo:
        held = self.lock_holders() if holders is None else holders
        rollout, archived = self._find_rollout(thread_id)
        cwd = turn_id = None
        last_event = None
        if rollout is not None:
            cwd = self._read_cwd(rollout)
            record = self._read_tail(rollout)
            payload = record.get("payload")
            if isinstance(payload, dict):
                raw_turn = payload.get("turn_id")
                turn_id = raw_turn if isinstance(raw_turn, str) else None
            stamp = record.get("timestamp")
            if isinstance(stamp, str):
                try:
                    last_event = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    last_event = None
        return ThreadInfo(
            thread_id=thread_id,
            name=self.display_name(thread_id),
            holder=held.get(thread_id),
            archived=archived,
            cwd=cwd,
            turn_id=turn_id,
            last_event=last_event,
            rollout=rollout,
        )

    def list_open(self) -> list[ThreadInfo]:
        """Threads Codex Desktop currently owns, newest activity first."""
        held = self.lock_holders()
        infos = [self.describe(tid, holders=held) for tid in held]
        return sorted(infos, key=lambda i: (i.age_seconds is None, i.age_seconds or 0.0))

    def describe_many(self, thread_ids: Iterable[str]) -> list[ThreadInfo]:
        held = self.lock_holders()
        return [self.describe(tid, holders=held) for tid in thread_ids]
