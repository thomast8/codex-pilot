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

The same "explicit rather than inherited" logic is why `model`, `effort` and
`service_tier` are per-call arguments here. Left alone, `codex exec` reads
`model`, `model_reasoning_effort` and `service_tier` from the instance's
`config.toml` -- a file Codex Desktop owns and rewrites wholesale, so what a
dispatch inherits changes without anyone asking. Editing that file to set up a
turn is the thing these arguments exist to stop: it races the app, and it moves
every other thread and interactive session at the same time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .instances import Instance
from .payloads import SERVICE_TIERS
from .threads import ThreadStore, serving_app

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
# Deliberately a shape and not an allowlist -- see `check_effort`. `-c` parses
# the right-hand side as TOML, so a bare token is also what keeps a second
# setting from riding in past the allowlists above.
EFFORT_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
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


def check_effort(effort: str) -> str:
    """Shape-check the reasoning effort reaching `-c model_reasoning_effort=`.

    Not an allowlist, unlike everything else here. The ladder is per model and
    comes from a server-side catalogue that changes with releases, so a fixed
    set would refuse a rung a newer model added -- and parking a thread one rung
    below what the work needed is the failure that would cause. What is refused
    is a value that could be read as more than a value.

    The CLI does not validate the rung either: `-c model_reasoning_effort=bogus`
    is accepted and printed straight back as `reasoning effort: bogus`. So a
    typo here does not fail, it dispatches at something unintended -- read
    `thread_status` back rather than assuming the rung took.
    """
    if not EFFORT_TOKEN.fullmatch(effort):
        raise DetachedError(
            f"effort must be a bare token like 'xhigh' or 'max', got {effort!r}. "
            "The rungs a model offers come from its own catalogue, so this is not "
            "checked against a list -- but a value the CLI would parse as more than "
            "an effort is refused."
        )
    return effort


def check_service_tier(tier: str) -> str:
    """Allowlist the service tier reaching `-c service_tier=`."""
    if tier not in SERVICE_TIERS:
        raise DetachedError(
            f"service_tier must be one of {sorted(SERVICE_TIERS)}, got {tier!r} "
            "('priority' is fast mode)"
        )
    return tier


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


def _setting_overrides(effort: str | None, service_tier: str | None) -> list[str]:
    """The `-c` pair for whichever of the two the caller named.

    Nothing is emitted for an argument left out: no default is invented here,
    so an unasked-for dispatch keeps whatever the instance is configured for
    rather than a rung this code picked.
    """
    argv: list[str] = []
    if effort is not None:
        argv += ["-c", f"model_reasoning_effort={check_effort(effort)}"]
    if service_tier is not None:
        argv += ["-c", f"service_tier={check_service_tier(service_tier)}"]
    return argv


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
        self._binary_override = codex_binary
        self.log_dir = log_dir or (instance.codex_home / LOG_DIR_NAME)

    @property
    def codex_binary(self) -> Path:
        """The codex of the bundle behind this home, else whatever is on PATH.

        The app that is *serving* the instance outranks the one that claims it.
        `app_path` is read off an Info.plist, and on a machine where a clone
        stamps the default home the claimant and the server are different apps
        -- the binary that wrote the rollout store is the one that should
        resume it, and the two bundles update independently.

        Unlike `link_target`, an unanswerable probe does not refuse here. An
        unaimed deep link *is* that bug, whereas resuming with the claimed
        bundle is at worst what this did before, and refusing every detached
        run because `ps` hiccuped would take out the route entirely. So both
        "nothing listening" and "could not tell" fall through to the claim.

        Resolved per access rather than cached at construction: a Session keeps
        one runner for its whole life, and over days the app quits, relaunches
        and updates underneath it. Two lsof sweeps per detached run is nothing
        beside resuming with the wrong build.
        """
        if self._binary_override is not None:
            return self._binary_override
        serving = serving_app(self.instance.socket_candidates()).bundle
        for app in (serving, self.instance.app_path):
            if app is None:
                continue
            bundled = app / "Contents" / "Resources" / "codex"
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
        effort: str | None = None,
        service_tier: str | None = None,
    ) -> DetachedRun:
        # Up front, because a refusal further down would come after the
        # unarchive: the thread would be moved out of the archive for a turn
        # that never ran. Each resume is its own process, so these have to be
        # passed again on every turn -- the previous run's settings are gone
        # with the process that carried them.
        if effort is not None:
            check_effort(effort)
        if service_tier is not None:
            check_service_tier(service_tier)

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
        argv += _setting_overrides(effort, service_tier)
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
        effort: str | None = None,
        service_tier: str | None = None,
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
        if effort is not None:
            check_effort(effort)
        if service_tier is not None:
            check_service_tier(service_tier)
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
        argv += _setting_overrides(effort, service_tier)
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
