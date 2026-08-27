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

  A holder is not automatically the app, and which it is decides the route. A
  detached `codex exec resume` -- ours or anyone's -- holds the same lock, and
  lsof reports its command name as "codex" exactly like the app's own
  `codex app-server` child, so the name cannot separate them. The pid can:
  the app's writer is the process listening on this instance's IPC socket, or
  a descendant of it. That test is decided by the process table rather than by
  argv, which matters because a detached run's argv contains the whole prompt
  and a prompt can say anything.
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
from typing import Any

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CWD_RE = re.compile(r'"cwd"\s*:\s*"([^"]+)"')
ROLLOUT_HEAD_LINES = 40
ROLLOUT_TAIL_BYTES = 65536

# Raw lsof over the lock files: thread id -> (pid, command). None, not an
# empty dict, when the probe could not run at all -- see `_lsof_locks`.
LockHolderProbe = Callable[[list[Path]], dict[str, tuple[int, str]] | None]
# The pids that make up the Codex Desktop instance serving one of these
# sockets. An empty set means no app is there; None means we could not find out.
AppProcessProbe = Callable[[list[Path]], set[int] | None]


class ThreadError(Exception):
    """Base for thread-resolution failures."""


class UnknownThreadError(ThreadError):
    """No thread matches the given name or id."""


class AmbiguousThreadError(ThreadError):
    """Several threads match; refuse rather than guess which one to drive."""


@dataclass(frozen=True)
class LockHolder:
    """The process holding one thread's writer lock.

    `is_app` is three-valued deliberately. True and False are answers; None
    means the classification could not be made, and collapsing that into either
    one sends a caller down a route that cannot work -- or, in the False
    direction, tells it a held thread is free to resume.
    """

    pid: int
    command: str
    is_app: bool | None

    def __str__(self) -> str:
        # The shape every message about a holder has always used.
        return f"{self.command}({self.pid})"

    @property
    def described(self) -> str:
        """Holder identity plus what we could work out about it, for messages."""
        if self.is_app is True:
            return f"{self} (Codex Desktop)"
        if self.is_app is False:
            return f"{self} (not Codex Desktop -- another writer, e.g. `codex exec`)"
        return f"{self} (could not tell whether this is Codex Desktop)"


@dataclass(frozen=True)
class LockCensus:
    """Who holds which writer lock, and whether that could be established.

    `known=False` is the case a plain dict cannot express: the probe failed, so
    an absent thread id means "not looked at", not "not locked".
    """

    holders: dict[str, LockHolder]
    known: bool = True

    @classmethod
    def unavailable(cls) -> LockCensus:
        return cls(holders={}, known=False)


@dataclass(frozen=True)
class ThreadInfo:
    thread_id: str
    name: str | None
    holder: LockHolder | None
    archived: bool
    cwd: str | None
    turn_id: str | None
    last_event: datetime | None
    rollout: Path | None
    # False when the lock probe could not run, so `holder is None` proves
    # nothing. Never let that state reach the detached route.
    lock_known: bool = True

    @property
    def app_owned(self) -> bool:
        """True when Codex Desktop holds the writer lock (drive it over IPC).

        Narrower than `locked`: a detached `codex exec` holds the lock too, and
        the IPC route cannot reach a thread it holds.
        """
        return self.holder is not None and self.holder.is_app is True

    @property
    def resumable(self) -> bool:
        """True when nothing holds the lock *and* we established that.

        The only condition under which `codex exec resume` may take the lock.
        """
        return self.lock_known and self.holder is None

    @property
    def age_seconds(self) -> float | None:
        if self.last_event is None:
            return None
        return (datetime.now(UTC) - self.last_event).total_seconds()


def default_codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def _run_lsof(paths: list[Path]) -> str | None:
    """lsof -F over some paths. None when it could not be run at all.

    A non-zero exit is not a failure here: lsof exits non-zero when nothing
    matches, which is the normal case for an app that is not running. Being
    unable to *run* lsof is a different answer entirely, and the two must not
    collapse -- an empty result read as "no locks held" would offer every
    thread to the detached route.
    """
    if not paths:
        return ""
    try:
        return subprocess.run(
            ["lsof", "-F", "pcn", *[str(p) for p in paths]],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _lsof_locks(paths: list[Path]) -> dict[str, tuple[int, str]] | None:
    """Map thread id -> (pid, command) for every lock file with an open fd."""
    out = _run_lsof(paths)
    if out is None:
        return None
    held: dict[str, tuple[int, str]] = {}
    pid = command = ""
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = value
        elif tag == "c":
            command = value
        elif tag == "n":
            stem = Path(value).stem
            if stem and pid.isdigit():
                held[stem] = (int(pid), command)
    return held


def _process_parents() -> dict[int, int] | None:
    """pid -> ppid for every process, or None if ps could not be run.

    One sweep, because the caller needs to walk a whole subtree and asking ps
    per pid would be a call per level per thread.
    """
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    parents: dict[int, int] = {}
    for line in out.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            parents[int(fields[0])] = int(fields[1])
    return parents or None


def _subtree(roots: set[int], parents: dict[int, int]) -> set[int]:
    """`roots` plus every process descended from one of them."""
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    seen = set(roots)
    queue = list(roots)
    while queue:
        for child in children.get(queue.pop(), ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def _app_processes(socket_paths: list[Path]) -> set[int] | None:
    """The pids that make up the Codex Desktop instance serving these sockets.

    This is what separates the app's writer from every other one. The process
    listening on the IPC socket is the app itself; the `codex app-server` child
    that actually holds the writer locks is a descendant of it. Verified live:
    lock holder 78315 (`.../ChatGPT.app/Contents/Resources/codex ... app-server`)
    is the child of 78222 (`.../ChatGPT.app/Contents/MacOS/ChatGPT`), which is
    the process lsof reports for the socket.

    Only the *listener* shows up in lsof for a socket path -- a connected
    client's fd is not bound to the pathname, verified with a client attached --
    so this set never accidentally swallows a caller of ours.

    Every candidate the instance might be listening on is offered, not just
    the canonical `$CODEX_HOME/ipc/ipc.sock`: the app bundle has a tmpdir
    fallback, and looking only at the canonical path there would find no
    listener and so classify the app's own writer as a foreign one -- which
    reads as `detached_running` and makes every verb refuse a thread that is
    perfectly driveable.

    An empty set is an answer: no app is serving any of these sockets, so
    anything holding a lock is another writer. None is not an answer: lsof or
    ps could not be run, and nothing may be concluded from that.
    """
    live: list[Path] = []
    for candidate in socket_paths:
        try:
            if candidate.is_socket():
                live.append(candidate)
        except OSError:
            return None
    if not live:
        return set()
    out = _run_lsof(live)
    if out is None:
        return None
    roots = {int(line[1:]) for line in out.splitlines() if line[:1] == "p" and line[1:].isdigit()}
    if not roots:
        return set()
    parents = _process_parents()
    if parents is None:
        return None
    return _subtree(roots, parents)


class ThreadStore:
    """Read-only view of one Codex instance's threads."""

    def __init__(
        self,
        codex_home: Path | None = None,
        lock_holder_probe: LockHolderProbe | None = None,
        app_process_probe: AppProcessProbe | None = None,
        socket_candidates: list[Path] | None = None,
    ) -> None:
        self.home = codex_home or default_codex_home()
        self._probe = lock_holder_probe or _lsof_locks
        self._app_probe = app_process_probe or _app_processes
        # Where this instance's app may be listening. `Instance` knows the full
        # list including the bundle's tmpdir fallback; the canonical path alone
        # is the right default for a store built without one.
        self._socket_candidates = socket_candidates or [self.socket_path]

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

    def exists(self, thread_id: str) -> bool:
        """Whether this instance actually holds the thread.

        Needed because a well-formed uuid looks valid against every instance.
        Without checking, a bare id appears to resolve everywhere, so the caller
        cannot tell which instance owns it -- and if only one instance happens to
        be live, the id binds to that one regardless of where it really lives.
        """
        rollout, _ = self._find_rollout(thread_id)
        if rollout is not None:
            return True
        return thread_id in self.names().values()

    def resolve(self, ref: str) -> str:
        """Turn a thread id, exact name, or unique substring into a thread id."""
        if UUID_RE.match(ref.lower()):
            thread_id = ref.lower()
            if not self.exists(thread_id):
                raise UnknownThreadError(f"no thread {thread_id} in {self.home}")
            return thread_id
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

    def lock_census(self) -> LockCensus:
        """Every held lock, with its holder classified as the app or not.

        Two probes rather than one: which pids hold locks, and which pids are
        the app. Either can fail, and the failures mean different things -- a
        lock probe that fails leaves the whole census unknown, while an app
        probe that fails leaves the holders known but unclassified.
        """
        if not self.locks_dir.is_dir():
            return LockCensus({})
        raw = self._probe(sorted(self.locks_dir.glob("*.lock")))
        if raw is None:
            return LockCensus.unavailable()
        if not raw:
            # Nothing holds a lock, so there is nothing to classify -- and the
            # app probe is the expensive half (an `lsof` plus a full `ps`).
            # Skipping it keeps the common case at one subprocess.
            return LockCensus({})
        app_pids = self._app_probe(self._socket_candidates)
        return LockCensus(
            {
                thread_id: LockHolder(
                    pid=pid,
                    command=command,
                    is_app=None if app_pids is None else pid in app_pids,
                )
                for thread_id, (pid, command) in raw.items()
            }
        )

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
    def _read_tail(path: Path) -> dict[str, Any]:
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
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                return record
        return {}

    # -- description --------------------------------------------------------

    def describe(self, thread_id: str, census: LockCensus | None = None) -> ThreadInfo:
        held = self.lock_census() if census is None else census
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
            holder=held.holders.get(thread_id),
            lock_known=held.known,
            archived=archived,
            cwd=cwd,
            turn_id=turn_id,
            last_event=last_event,
            rollout=rollout,
        )

    def list_open(self) -> list[ThreadInfo]:
        """Threads something currently holds a writer lock on, newest first.

        Not only the app's: a detached run holds a lock too, and a caller
        deciding what is driveable needs to see it rather than have it hidden.
        Read `app_owned` on each to tell which writer it is.
        """
        held = self.lock_census()
        infos = [self.describe(tid, census=held) for tid in held.holders]
        return sorted(infos, key=lambda i: (i.age_seconds is None, i.age_seconds or 0.0))

    def describe_many(self, thread_ids: Iterable[str]) -> list[ThreadInfo]:
        held = self.lock_census()
        return [self.describe(tid, census=held) for tid in thread_ids]
