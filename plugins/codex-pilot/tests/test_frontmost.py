"""Putting the user's app back after a deep link raises Codex.

The app raises its own window whenever it handles a `codex://` link, so the
only thing left to us is undoing it. Every case here is about *not* undoing it
when we cannot prove we caused the change -- reactivating the wrong app would
be a worse interruption than the one we are fixing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from codex_pilot.frontmost import LSAPPINFO, OPEN, FrontmostGuard, parse_bundle_path

CODEX = Path("/Applications/ChatGPT.app")
OTHER_CODEX = Path("/Users/x/Applications/ChatGPT Personal.app")
MAIL = Path("/System/Applications/Mail.app")
ZED = Path("/Applications/Zed.app")

FRONT_ASN = "ASN:0x0-0xcc0cc:"

LSAPPINFO_INFO = """"Mail" ASN:0x0-0xcc0cc: (in front)
    bundleID=[ NULL ]
    bundle path=[ NULL ]

[ NULL ]  ASN:0x0-0xcc0cc: (in front)
    bundle path="{path}"
    executable path=[ NULL ]
"""


class FakeRunner:
    """Stands in for the three shell-outs: front asn, bundle path, activate."""

    ASN = "ASN:0x0-0xcc0cc:"

    def __init__(
        self,
        fronts: list[Path | None],
        activate_ok: bool = True,
        activate_takes: bool = True,
    ) -> None:
        self.fronts = list(fronts)
        self.activate_ok = activate_ok
        # Whether `open -a` actually wins the foreground. False models the case
        # the guard must not paper over: the command succeeded, the app did not
        # come forward.
        self.activate_takes = activate_takes
        self.calls: list[list[str]] = []
        self.activated: list[str] = []
        self.pending: Path | None = None

    def _front(self) -> Path | None:
        # Last value repeats, so a test only lists the transitions it cares about.
        return self.fronts.pop(0) if len(self.fronts) > 1 else self.fronts[0]

    def __call__(self, argv: list[str]) -> str | None:
        self.calls.append(argv)
        if argv[:2] == [LSAPPINFO, "front"]:
            self.pending = self._front()
            return None if self.pending is None else FRONT_ASN
        if argv[:2] == [LSAPPINFO, "info"]:
            # The ASN from the previous call has to be the one asked about;
            # answering from a cache regardless would hide a lookup that
            # queried the wrong app.
            assert argv[-1] == FRONT_ASN.strip(), f"queried {argv[-1]!r}, not the front ASN"
            return None if self.pending is None else LSAPPINFO_INFO.format(path=self.pending)
        if argv[0] == OPEN:
            self.activated.append(argv[-1])
            if self.activate_ok and self.activate_takes and argv[1:2] == ["-a"]:
                self.fronts = [Path(argv[-1])]
            return "" if self.activate_ok else None
        raise AssertionError(f"unexpected argv {argv}")


def fake_clock(limit: int = 200) -> tuple[Callable[[float], None], Callable[[], float]]:
    """A clock only `sleep` advances, and that refuses to be read forever.

    The cap turns "the poll loop stopped pacing itself" from a hung test suite
    into a failed assertion.
    """
    state = {"t": 0.0, "reads": 0}

    def sleep(seconds: float) -> None:
        state["t"] += seconds

    def now() -> float:
        state["reads"] += 1
        if state["reads"] > limit:
            raise AssertionError("polled the clock without ever advancing it")
        return state["t"]

    return sleep, now


def guard(runner: FakeRunner, apps: set[Path] | None = None) -> FrontmostGuard:
    """A guard on a fake clock, so a test that waits out a deadline is instant."""
    sleep, now = fake_clock()
    return FrontmostGuard(
        apps if apps is not None else {CODEX},
        run=runner,
        sleep=sleep,
        now=now,
        timeout=0.5,
        interval=0.1,
    )


def test_parses_the_bundle_path_out_of_lsappinfo() -> None:
    assert parse_bundle_path(LSAPPINFO_INFO.format(path=MAIL)) == MAIL


def test_parses_nothing_from_an_entry_with_no_bundle_path() -> None:
    assert parse_bundle_path('"Mail" ASN:0x0-1: (in front)\n    bundle path=[ NULL ]\n') is None


def test_restores_the_app_that_was_in_front() -> None:
    runner = FakeRunner([MAIL, CODEX])
    with guard(runner) as g:
        pass
    assert g.outcome == {"restored": True, "app": str(MAIL)}
    assert runner.activated == [str(MAIL)]


def test_restores_the_other_codex_app_when_that_is_where_the_user_was() -> None:
    # Two instances: the user is in one, we raise the other. "A Codex app is in
    # front" is not the test -- the app we are raising is.
    runner = FakeRunner([OTHER_CODEX, CODEX])
    with guard(runner, {CODEX}) as g:
        pass
    assert g.outcome["restored"] is True
    assert runner.activated == [str(OTHER_CODEX)]


def test_does_not_restore_when_the_user_moved_somewhere_else() -> None:
    # Codex came forward, the user went to Zed, and yanking them back out of it
    # would be the same rudeness we are trying to undo.
    runner = FakeRunner([MAIL, ZED])
    with guard(runner) as g:
        pass
    assert g.outcome == {"restored": False, "reason": "not_raised", "waited_seconds": 0.5}
    assert runner.activated == []


def test_does_not_restore_when_codex_was_already_in_front() -> None:
    runner = FakeRunner([CODEX])
    with guard(runner) as g:
        pass
    assert g.outcome == {"restored": False, "reason": "already_frontmost"}
    assert runner.activated == []


def test_says_so_when_the_frontmost_app_could_not_be_read() -> None:
    # lsappinfo missing or refusing: not a licence to guess at what to reactivate.
    runner = FakeRunner([None])
    with guard(runner) as g:
        pass
    assert g.outcome == {"restored": False, "reason": "frontmost_unknown"}
    assert runner.activated == []


def test_says_so_when_reactivating_failed() -> None:
    runner = FakeRunner([MAIL, CODEX], activate_ok=False)
    with guard(runner) as g:
        pass
    assert g.outcome == {"restored": False, "reason": "activate_failed", "app": str(MAIL)}


def test_reports_no_known_bundle_rather_than_reactivating_blind() -> None:
    # With no app path for the instance we cannot tell "Codex raised itself"
    # from "the user switched apps", so we do nothing and say why.
    runner = FakeRunner([MAIL, ZED])
    with guard(runner, set()) as g:
        pass
    assert g.outcome == {"restored": False, "reason": "no_known_bundle"}
    assert runner.activated == []


def test_waits_for_the_raise_rather_than_giving_up_on_the_first_look() -> None:
    # The app raises a beat after `open` returns; polling is the whole point.
    runner = FakeRunner([MAIL, MAIL, MAIL, CODEX])
    with guard(runner) as g:
        pass
    assert g.outcome["restored"] is True


def test_one_restore_covers_a_batch_of_focuses() -> None:
    runner = FakeRunner([MAIL, CODEX])
    with guard(runner) as g:
        for _ in range(3):
            runner([OPEN, "-g", "codex://threads/x"])
    assert g.outcome["restored"] is True
    assert runner.activated == ["codex://threads/x"] * 3 + [str(MAIL)]


def test_an_exception_inside_the_guard_still_restores_and_propagates() -> None:
    runner = FakeRunner([MAIL, CODEX])
    g = guard(runner)
    try:
        with g:
            raise RuntimeError("open blew up")
    except RuntimeError:
        pass
    else:  # pragma: no cover - the guard must not swallow it
        raise AssertionError("the guard swallowed the exception")
    assert g.outcome["restored"] is True


def test_paces_its_polling_and_stops_at_the_deadline() -> None:
    """The wait is a paced poll, not a spin.

    Driven by a fake clock that only the fake sleep advances, so deleting the
    sleep would leave the deadline unreachable rather than quietly hammering
    `lsappinfo` in production -- and `now` refuses to be asked forever.
    """
    runner = FakeRunner([MAIL, ZED])
    sleeps: list[float] = []
    tick, now = fake_clock(limit=50)

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        tick(seconds)

    g = FrontmostGuard({CODEX}, run=runner, sleep=sleep, now=now, timeout=0.5, interval=0.1)
    with g:
        pass
    assert g.outcome == {"restored": False, "reason": "not_raised", "waited_seconds": 0.5}
    assert sleeps == [0.1] * 5, "expected one sleep per interval up to the deadline"


def test_a_missed_raise_says_how_long_it_actually_waited() -> None:
    """`not_raised` alone cannot be acted on, because it has two causes.

    Either the user moved somewhere else, or the app was slower than the window
    we gave it -- and which one it was depends entirely on how long that window
    was. A cold launch gets a longer one than a warm app, so the number has to
    travel with the outcome rather than be inferred from a constant the reader
    would have to guess at.
    """
    runner = FakeRunner([MAIL, ZED])
    sleep, now = fake_clock(limit=200)
    g = FrontmostGuard({CODEX}, run=runner, sleep=sleep, now=now, timeout=15.0, interval=0.1)
    with g:
        pass
    assert g.outcome == {"restored": False, "reason": "not_raised", "waited_seconds": 15.0}


def test_a_longer_deadline_really_does_wait_longer() -> None:
    """The cold-launch window is only worth having if the raise still lands.

    An app starting from closed can take several seconds to show a window; the
    3s default gives up before then and the interruption is never undone. With
    the longer deadline the same late raise is caught and restored.
    """
    # Codex arrives on the 40th look -- 4s in, past the warm deadline.
    fronts: list[Path | None] = [MAIL] * 40 + [CODEX]
    runner = FakeRunner(fronts)
    sleep, now = fake_clock(limit=400)
    g = FrontmostGuard({CODEX}, run=runner, sleep=sleep, now=now, timeout=15.0, interval=0.1)
    with g:
        pass
    assert g.outcome == {"restored": True, "app": str(MAIL)}


def test_installed_apps_finds_every_bundle_under_the_search_dirs(tmp_path) -> None:
    from codex_pilot.instances import installed_apps

    first, second = tmp_path / "Applications", tmp_path / "home-apps"
    for directory, names in (
        (first, ["ChatGPT.app", "ChatGPT Personal.app"]),
        (second, ["ChatGPT Work.app"]),
    ):
        directory.mkdir()
        for name in names:
            (directory / name).mkdir()
    (first / "Safari.app").mkdir()

    found = installed_apps([first, second, tmp_path / "gone"])

    assert [p.name for p in found] == ["ChatGPT Personal.app", "ChatGPT.app", "ChatGPT Work.app"]


def test_does_not_claim_a_restore_the_activation_never_won() -> None:
    # `open -a` returning 0 is not the app being in front. Saying "restored"
    # on the strength of the command alone is the benign-looking default.
    runner = FakeRunner([MAIL, CODEX], activate_takes=False)
    with guard(runner) as g:
        pass
    assert g.outcome == {"restored": False, "reason": "not_confirmed", "app": str(MAIL)}
    assert runner.activated == [str(MAIL)] * 2, "expected one retry before giving up"


def test_retries_when_a_second_codex_window_rises_after_the_restore() -> None:
    """A sweep fires several links and they do not land together.

    The first app raises, the restore lands, and then the second app's own
    raise arrives on top of it -- so the guard tries once more rather than
    reporting a restore the user cannot see.
    """
    other = Path("/Applications/ChatGPT Second.app")
    runner = FakeRunner([MAIL, CODEX])
    activations: list[int] = []
    original = runner.__call__

    def run(argv):
        if argv[0] == OPEN and argv[1:2] == ["-a"]:
            activations.append(len(activations))
            if len(activations) == 1:
                # The restore is issued, then the other window steals it back.
                runner.calls.append(argv)
                runner.activated.append(argv[-1])
                runner.fronts = [other]
                return ""
        return original(argv)

    sleep, now = fake_clock()
    g = FrontmostGuard({CODEX, other}, run=run, sleep=sleep, now=now, timeout=0.5, interval=0.1)
    with g:
        pass
    assert g.outcome == {"restored": True, "app": str(MAIL)}
    assert runner.activated == [str(MAIL)] * 2
