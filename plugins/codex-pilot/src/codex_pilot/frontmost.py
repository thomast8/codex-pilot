"""Give the screen back after a `codex://` link raises Codex Desktop.

Surfacing a thread costs the user their foreground app, and there is no way to
ask for it politely. The app's `open-url` handler awaits
`ensurePrimaryWindowVisible` -- `restore()`, `show()`, `focus()` on the primary
window -- *before* it navigates, so every deep link raises the window whatever
the caller does. `open -g` suppresses only macOS's launch-side activation.

What is left is undoing it: remember who was in front, fire the link, wait for
Codex to actually come forward, then reactivate the app we displaced. The whole
design constraint is the wait: restore too eagerly and we reactivate before the
raise, restore unconditionally and we drag the user out of wherever they moved
in the meantime. So a restore happens only when we can *prove* Codex is the app
in front, and every other outcome is reported with a reason rather than passed
off as success.

`lsappinfo` is the probe because it needs no accessibility grant, unlike
System Events. It is a macOS binary; anywhere else it is simply absent, which
reads as "front unknown" and skips the restore.

One caveat about the paths it reports: they are not always the path you would
guess. Safari comes back under `/System/Volumes/Preboot/Cryptexes/App/...`
rather than `/Applications`, and `open -a` accepts that form back (observed
2026-08-28). Recognising the raise is a set membership test against the bundles
the caller says it is about to raise -- the link is aimed with `open -a`, so
that is exactly one app per instance -- and if macOS ever reported one of those
under a different normalisation the raise would go unrecognised and nothing
would be restored: the safe direction, and it has matched on every real run so
far.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from types import TracebackType
from typing import Any

# stdout of a successful command, or None if it could not be run at all. The
# distinction matters: an empty answer is a fact, a failure is not.
Runner = Callable[[list[str]], "str | None"]

BUNDLE_PATH = re.compile(r'bundle path="([^"]+)"')

# How long the app may take to raise itself after `open` returns. Measured well
# under a second on a warm app; the ceiling is for a cold or busy one, and
# exceeding it costs nothing but a skipped restore.
RAISE_TIMEOUT_SECONDS = 3.0
POLL_SECONDS = 0.1

# Reactivation is asynchronous too, so "restored" is only reported once the app
# is observed back in front. A batch can raise a second Codex window after the
# first restore lands, which is what the retry is for; beyond that we stop
# rather than fight whatever is winning the foreground.
CONFIRM_TIMEOUT_SECONDS = 1.0
ACTIVATE_ATTEMPTS = 2

# Absolute, so the probe cannot be shadowed by whatever is early on PATH. Both
# ship with macOS. Note that `lsappinfo` rejects a `--` argument terminator
# outright ("Unrecognized command"), measured, so the ASN goes through as a
# plain positional.
LSAPPINFO = "/usr/bin/lsappinfo"
OPEN = "/usr/bin/open"


def _run(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def parse_bundle_path(text: str) -> Path | None:
    """Pull the bundle path out of `lsappinfo info -only bundlepath` output.

    The command prints the app twice, once with `bundle path=[ NULL ]` and once
    with the real value, so match the quoted form and ignore the rest.
    """
    match = BUNDLE_PATH.search(text)
    return Path(match.group(1)) if match else None


def frontmost(run: Runner = _run) -> Path | None:
    """Bundle path of the app currently in front, or None if it cannot be read."""
    asn = run([LSAPPINFO, "front"])
    if not asn or not asn.strip():
        return None
    info = run([LSAPPINFO, "info", "-only", "bundlepath", asn.strip()])
    return parse_bundle_path(info) if info else None


class FrontmostGuard:
    """Restore the foreground app around one or more deep-link opens.

    Wrap a whole batch rather than each link: focusing five threads should cost
    the user one flash, not five.
    """

    def __init__(
        self,
        targets: Iterable[Path],
        run: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        timeout: float = RAISE_TIMEOUT_SECONDS,
        interval: float = POLL_SECONDS,
        confirm_timeout: float = CONFIRM_TIMEOUT_SECONDS,
    ) -> None:
        # The bundles this guard is about to raise -- not every Codex app on
        # the machine. A user sitting in a *different* instance is someone to
        # restore, not someone already where we are sending them.
        self._apps = {Path(app) for app in targets}
        # Resolved here rather than as a default argument so that patching
        # the module-level runner in a test actually takes effect.
        self._run = run if run is not None else _run
        self._sleep = sleep
        self._now = now
        self._timeout = timeout
        self._interval = interval
        self._confirm_timeout = confirm_timeout
        self.previous: Path | None = None
        self.outcome: dict[str, Any] = {"restored": False, "reason": "not_run"}

    def __enter__(self) -> FrontmostGuard:
        if not self._apps:
            # Without a bundle path for the app we are about to raise, a change
            # of frontmost app cannot be attributed to us, and reactivating on
            # that basis would fight the user rather than help.
            self.outcome = {"restored": False, "reason": "no_known_bundle"}
            return self
        self.previous = frontmost(self._run)
        if self.previous is None:
            self.outcome = {"restored": False, "reason": "frontmost_unknown"}
        elif self.previous in self._apps:
            self.outcome = {"restored": False, "reason": "already_frontmost"}
        else:
            self.outcome = {"restored": False, "reason": "not_raised"}
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Runs on the exception path too: the link may well have been fired
        # before whatever went wrong, so the screen still needs giving back.
        if self.outcome["reason"] != "not_raised" or self.previous is None:
            return
        if not self._wait_for_raise():
            return
        target = str(self.previous)
        for _ in range(ACTIVATE_ATTEMPTS):
            if self._run([OPEN, "-a", target]) is None:
                self.outcome = {"restored": False, "reason": "activate_failed", "app": target}
                return
            if self._confirm():
                self.outcome = {"restored": True, "app": target}
                return
            # Another Codex window can rise after the restore -- a batch fires
            # several links and they do not land together. That is worth one
            # more attempt; anything else in front belongs to the user.
            if frontmost(self._run) not in self._apps:
                break
        self.outcome = {"restored": False, "reason": "not_confirmed", "app": target}

    def _confirm(self) -> bool:
        """True once the app we reactivated is actually the one in front.

        Issuing the activation is not the same as it taking effect, and
        reporting `restored: true` on the strength of a command having been
        run would be exactly the kind of benign-looking default this codebase
        keeps finding in itself.
        """
        deadline = self._now() + self._confirm_timeout
        while True:
            if frontmost(self._run) == self.previous:
                return True
            if self._now() >= deadline:
                return False
            self._sleep(self._interval)

    def _wait_for_raise(self) -> bool:
        """True once one of our Codex bundles is the app in front.

        False means we never saw it come forward -- either it has not yet, or
        the user has already moved on to something else. Both are reasons to
        leave the foreground alone.

        The deadline bounds the polling, not each probe: a wedged `lsappinfo`
        can stretch one pass by its own 5s subprocess timeout, so the true
        ceiling is the deadline plus one probe rather than the deadline.
        """
        deadline = self._now() + self._timeout
        while True:
            if frontmost(self._run) in self._apps:
                return True
            if self._now() >= deadline:
                return False
            self._sleep(self._interval)
