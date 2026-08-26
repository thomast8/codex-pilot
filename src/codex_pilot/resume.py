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
LOG_DIR_NAME = "codex-pilot-logs"


class DetachedError(Exception):
    """The detached route could not be taken."""


class LockedError(DetachedError):
    """Something already holds this thread's writer lock.

    Almost always Codex Desktop, in which case the IPC route is the one to use.
    Never work around this: two writers on one rollout corrupt it, which is
    exactly what the lock exists to prevent.
    """


@dataclass
class DetachedRun:
    """A `codex exec resume` running in the background."""

    thread_id: str
    instance: str
    cwd: Path
    log_path: Path
    process: subprocess.Popen[bytes]
    unarchived: bool = False

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
        if info.app_owned:
            raise LockedError(
                f"{thread_id} is held by {info.holder} -- use the IPC route while the app "
                "has it open, or close the thread in the app to free it"
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
            sandbox,
            "-c",
            f"approval_policy={approval}",
        ]
        if model is not None:
            argv += ["--model", model]
        argv += ["--skip-git-repo-check", "resume", thread_id, text]

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

        return DetachedRun(
            thread_id=thread_id,
            instance=self.instance.slug,
            cwd=cwd,
            log_path=log_path,
            process=process,
            unarchived=unarchived,
        )

    def wait_for_completion(self, run: DetachedRun, timeout: float = 300.0) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = run.returncode
            if code is not None:
                return code
            time.sleep(0.5)
        return None
