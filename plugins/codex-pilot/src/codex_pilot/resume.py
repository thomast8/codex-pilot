"""The detached route: driving a thread no Codex Desktop window owns.

The IPC path only reaches threads the app currently has open. The other
direction is `codex exec resume`, which restores a thread's full history from
disk and continues it in place under the same id -- but only if nothing else
holds its writer lock. The two routes are exclusive by construction, and that is
the safety property: we never contend for the lock, we pick whichever route the
lock state permits.

Three things this gets right that a bare subprocess call would not:

- **The binary follows the instance.** Codex Desktop bundles its own `codex`,
  which here is ahead of the one on PATH (0.149.0-alpha.4.3 vs 0.147.0) and is
  the build that wrote the rollout. Resuming with an older build risks reading a
  store it does not fully understand, so the instance's own binary wins.
- **`CODEX_HOME` is set explicitly.** Everything -- the store, the locks, the
  archive -- is per instance, so an inherited `CODEX_HOME` would quietly write
  the turn into the wrong instance.
- **Approvals cannot be answered.** A detached run has no TTY, so an
  `on-request` policy stalls until it times out. The policy is therefore always
  explicit rather than inherited from config.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .instances import Instance
from .threads import ThreadStore

DEFAULT_SANDBOX = "workspace-write"
DEFAULT_APPROVAL = "never"

# Both reach the CLI as security controls -- `--sandbox <v>` and
# `-c approval_policy=<v>` -- so they are allowlisted rather than passed
# through. The caller is a model, and "danger-full-access" plus an
# approval policy of "never" is an unsupervised agent with no sandbox.
# Opt into that deliberately via CODEX_PILOT_ALLOW_FULL_ACCESS, not by
# choosing a string.
SANDBOX_MODES = frozenset({"read-only", "workspace-write"})
PRIVILEGED_SANDBOX = "danger-full-access"
FULL_ACCESS_ENV = "CODEX_PILOT_ALLOW_FULL_ACCESS"
APPROVAL_POLICIES = frozenset({"untrusted", "on-failure", "on-request", "never"})
LOG_DIR_NAME = "codex-pilot-logs"

# `codex exec --json` announces the thread it created as its first JSON line.
THREAD_STARTED_EVENT = "thread.started"
THREAD_ID_WAIT = 15.0
ID_POLL_INTERVAL = 0.05


def scan_for_thread_id(log_path: Path) -> str | None:
    """First `thread.started` id in a `codex exec --json` log, if it is there yet.

    Tolerates partial and non-JSON lines: the CLI prints a human preamble before
    the JSONL stream, and a line can be half-written when we look.
    """
    try:
        with log_path.open(errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("type") == THREAD_STARTED_EVENT:
                    thread_id = record.get("thread_id")
                    if isinstance(thread_id, str):
                        return thread_id
    except OSError:
        return None
    return None


class DetachedError(Exception):
    """The detached route could not be taken."""


class LockedError(DetachedError):
    """This thread's writer lock is held, or could not be shown to be free.

    Codex Desktop when the thread is open in the app, in which case the IPC
    route is the one to use -- but equally another `codex exec`, ours or
    anyone's, and then neither route reaches it until that process exits.
    Raised for an unprobeable lock too: not knowing is not the same as free.
    Never work around this: two writers on one rollout corrupt it, which is
    exactly what the lock exists to prevent.
    """


def check_sandbox(sandbox: str) -> str:
    """Allowlist the sandbox mode; gate full access behind an env opt-in."""
    if sandbox in SANDBOX_MODES:
        return sandbox
    if sandbox == PRIVILEGED_SANDBOX:
        if os.environ.get(FULL_ACCESS_ENV):
            return sandbox
        raise DetachedError(
            f"sandbox {PRIVILEGED_SANDBOX!r} runs an agent with no sandbox at all; "
            f"it is refused unless {FULL_ACCESS_ENV} is set in the server's environment"
        )
    raise DetachedError(f"sandbox must be one of {sorted(SANDBOX_MODES)}, got {sandbox!r}")


def check_approval(approval: str) -> str:
    """Allowlist the approval policy reaching `-c approval_policy=`."""
    if approval not in APPROVAL_POLICIES:
        raise DetachedError(
            f"approval must be one of {sorted(APPROVAL_POLICIES)}, got {approval!r}"
        )
    return approval


def check_prompt(text: str) -> str:
    """Refuse a prompt the CLI would read as something other than a prompt.

    A bare `-` means "read the prompt from stdin", and stdin is DEVNULL here, so
    it would silently start a thread with no prompt at all.
    """
    if text.strip() == "-":
        raise DetachedError(
            "a prompt of '-' means 'read from stdin', which a detached run has none of"
        )
    return text


@dataclass
class DetachedRun:
    """A `codex exec resume` running in the background."""

    thread_id: str | None
    instance: str
    cwd: Path
    log_path: Path
    process: subprocess.Popen[bytes]
    unarchived: bool = False
    # Set once this run's exit has been turned into an event, so the pump
    # announces each run exactly once.
    reported: bool = False
    # Set when we terminated it ourselves, so its non-zero exit is not
    # reported as though the agent had failed on its own.
    stopped: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def as_dict(self) -> dict[str, object]:
        return {
            "route": "detached",
            "instance": self.instance,
            "thread": self.thread_id,
            "pid": self.pid,
            "cwd": str(self.cwd),
            "log_path": str(self.log_path),
            "unarchived": self.unarchived,
            "running": self.running,
        }


class DetachedRunner:
    """Runs turns on threads the app does not own, for one instance."""

    def __init__(
        self,
        instance: Instance,
        store: ThreadStore,
        codex_binary: Path | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.instance = instance
        self.store = store
        self.codex_binary = codex_binary or self._default_binary(instance)
        self.log_dir = log_dir or (instance.codex_home / LOG_DIR_NAME)

    @staticmethod
    def _default_binary(instance: Instance) -> Path:
        """The instance's own codex, else whatever is on PATH."""
        if instance.app_path is not None:
            bundled = instance.app_path / "Contents" / "Resources" / "codex"
            if bundled.is_file() and os.access(bundled, os.X_OK):
                return bundled
        return Path("codex")

    def _unarchive(self, thread_id: str) -> None:
        result = subprocess.run(
            [str(self.codex_binary), "unarchive", thread_id],
            capture_output=True,
            text=True,
            env={**os.environ, "CODEX_HOME": str(self.instance.codex_home)},
            check=False,
        )
        if result.returncode != 0:
            raise DetachedError(f"could not unarchive {thread_id}: {result.stderr.strip()}")

    def run(
        self,
        thread_id: str,
        text: str,
        sandbox: str = DEFAULT_SANDBOX,
        approval: str = DEFAULT_APPROVAL,
        model: str | None = None,
    ) -> DetachedRun:
        info = self.store.describe(thread_id)
        if info.rollout is None:
            raise DetachedError(f"no thread {thread_id} in instance {self.instance.slug!r}")

        # Checked immediately before spawning, but this is advisory: the CLI
        # takes the lock itself and is the authoritative arbiter of the race.
        #
        # Keyed on `resumable`, which is narrower than "the app does not own
        # it" in both directions that matter. A `codex exec` holding the lock
        # is not the app and must still block us -- two of them on one rollout
        # is the corruption the lock exists to prevent, and it does not become
        # safe because the other writer is not Codex Desktop. And a lock state
        # we could not probe has not been established as free.
        if not info.resumable:
            if info.holder is not None:
                raise LockedError(
                    f"{thread_id} is held by {info.holder.described} -- use the IPC route "
                    "while Codex Desktop has it open, close the thread in the app to free "
                    "it, or wait for another writer to exit"
                )
            raise LockedError(
                f"the writer lock on {thread_id} could not be probed, so whether anything "
                "holds it is unknown -- refusing to resume rather than risk a second writer "
                "on the rollout. Check that `lsof` is usable and retry."
            )

        unarchived = False
        if info.archived:
            self._unarchive(thread_id)
            unarchived = True
            info = self.store.describe(thread_id)

        if not info.cwd or not Path(info.cwd).is_dir():
            raise DetachedError(
                f"thread cwd {info.cwd!r} is missing; refusing to run the turn somewhere else"
            )
        cwd = Path(info.cwd)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{thread_id}-{uuid.uuid4().hex[:8]}.log"

        argv = [
            str(self.codex_binary),
            "exec",
            "--sandbox",
            check_sandbox(sandbox),
            "-c",
            f"approval_policy={check_approval(approval)}",
        ]
        if model is not None:
            argv += ["--model", model]
        # `--` so a prompt starting with `-` is a prompt and not a flag: without
        # it the CLI parses `--help` (or any typo) as an option and the turn
        # never runs.
        argv += ["--skip-git-repo-check", "resume", thread_id, "--", check_prompt(text)]

        process = self._spawn(argv, cwd, log_path)
        return DetachedRun(
            thread_id=thread_id,
            instance=self.instance.slug,
            cwd=cwd,
            log_path=log_path,
            process=process,
            unarchived=unarchived,
        )

    def start(
        self,
        text: str,
        cwd: Path,
        sandbox: str = DEFAULT_SANDBOX,
        approval: str = DEFAULT_APPROVAL,
        model: str | None = None,
        wait_for_id: float = THREAD_ID_WAIT,
    ) -> DetachedRun:
        """Create a brand-new thread and run its first turn in `cwd`.

        There is no IPC method for this -- the app's follower surface can only
        drive threads that already exist -- so a new thread has to come from the
        CLI. Spawning it detached rather than waiting on it is the whole point:
        the call returns as soon as the thread has an id, while the agent keeps
        working.

        `cwd` is required and passed as `--cd`. A new thread otherwise inherits
        whatever directory the caller happened to be in, which is how work ends
        up in the wrong repo or outside the worktree it was meant for.
        """
        check_sandbox(sandbox)
        check_approval(approval)
        check_prompt(text)
        cwd = cwd.expanduser().resolve()
        if not cwd.is_dir():
            raise DetachedError(
                f"cwd {str(cwd)!r} is not a directory -- create the worktree or "
                "directory first, then start the thread in it"
            )

        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"new-{uuid.uuid4().hex[:8]}.log"

        argv = [
            str(self.codex_binary),
            "exec",
            # JSONL on stdout is the only way to learn the id of the thread the
            # CLI just created; without it the thread is unreachable afterwards.
            "--json",
            "--sandbox",
            check_sandbox(sandbox),
            "-c",
            f"approval_policy={check_approval(approval)}",
        ]
        if model is not None:
            argv += ["--model", model]
        argv += ["--cd", str(cwd), "--skip-git-repo-check", "--", check_prompt(text)]

        process = self._spawn(argv, cwd, log_path)
        return DetachedRun(
            thread_id=self._await_thread_id(log_path, process, wait_for_id),
            instance=self.instance.slug,
            cwd=cwd,
            log_path=log_path,
            process=process,
        )

    def _spawn(self, argv: list[str], cwd: Path, log_path: Path) -> subprocess.Popen[bytes]:
        log = log_path.open("wb")
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "CODEX_HOME": str(self.instance.codex_home)},
                start_new_session=True,
            )
        except OSError as exc:
            raise DetachedError(f"could not start {self.codex_binary}: {exc}") from exc
        finally:
            # The child holds its own dup of this fd, so closing the parent's
            # copy still lets the log fill. Relying on refcounting to do it
            # emits a ResourceWarning and would genuinely leak elsewhere.
            log.close()
        return process

    @staticmethod
    def _await_thread_id(
        log_path: Path, process: subprocess.Popen[bytes], timeout: float
    ) -> str | None:
        """Poll the log for the `thread.started` event, briefly.

        Returns None rather than raising when it does not arrive: the run is
        already spawned, so the caller still needs its pid and log path to find
        out what went wrong.
        """
        deadline = time.monotonic() + timeout
        while True:
            found = scan_for_thread_id(log_path)
            if found is not None:
                return found
            if process.poll() is not None:
                # Exited: stdout went straight to the fd, so nothing more is coming.
                return scan_for_thread_id(log_path)
            if time.monotonic() >= deadline:
                return None
            time.sleep(ID_POLL_INTERVAL)

    def wait_for_completion(self, run: DetachedRun, timeout: float = 300.0) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = run.returncode
            if code is not None:
                return code
            time.sleep(0.5)
        return None
