"""What the follow pump does when its own code goes wrong.

The pump catches broadly so a bad frame cannot kill it, and that is right: a
dead pump leaves every follow silently unmaintained with nothing left to
recover it. But the same catch swallowed a real defect once -- a method that had
gone missing raised AttributeError on every tick, and the pump spun at 1Hz
reporting nothing while follows quietly stopped being re-subscribed.

So the rule these pin is: survive, but never silently. An error the pump did not
expect must reach the caller through the same channel everything else does.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from codex_pilot.actions import Session
from codex_pilot.instances import Instance
from fakeapp import FakeApp, wait_until, write_index

THREAD = "01a03e2b-a106-77a3-add2-913ac3f7336a"


@pytest.fixture
def live():
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        app = FakeApp(home)
        instance = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
        session = Session(instances=[instance])
        try:
            yield app, session, instance
        finally:
            session.close()
            app.close()


def health_of(session: Session, thread: str) -> dict:
    return session.collect_events(threads=[thread])["threads"][thread]


def test_an_unexpected_error_in_the_pump_reaches_the_caller(live, monkeypatch):
    """The failure that started this: silent forever, at one tick per second."""
    app, session, instance = live
    session.follow_thread(THREAD)
    assert wait_until(lambda: THREAD in app.wait_for_connection(1).followed_threads())

    def boom(_older_than):
        raise AttributeError("stale_awaiting went missing")

    monkeypatch.setattr(session.follow_manager(instance), "stale_awaiting", boom)

    assert wait_until(lambda: health_of(session, THREAD)["health"] == "lost"), (
        "a pump fault left the follow looking healthy"
    )
    reason = health_of(session, THREAD)["reason"] or ""
    assert "AttributeError" in reason and "stale_awaiting went missing" in reason
    assert health_of(session, THREAD)["pending_known"] is False


def test_the_pump_survives_the_fault_and_recovers(live, monkeypatch):
    """Surviving is the other half: a pump that dies cannot re-subscribe anything."""
    app, session, instance = live
    manager = session.follow_manager(instance)
    session.follow_thread(THREAD)
    wait_until(lambda: THREAD in app.wait_for_connection(1).followed_threads())

    real = manager.stale_awaiting
    failing = {"on": True}

    def sometimes(older_than):
        if failing["on"]:
            raise RuntimeError("transient")
        return real(older_than)

    monkeypatch.setattr(manager, "stale_awaiting", sometimes)
    assert wait_until(lambda: health_of(session, THREAD)["health"] == "lost")

    failing["on"] = False
    assert session._pumps["default"].is_alive(), (
        "the pump died on an error it was supposed to survive"
    )
    # It has to actually resume work, not merely stay alive.
    manager.resync_all("after the fault")
    assert wait_until(lambda: THREAD in app.wait_for_connection(1).followed_threads())


def test_a_repeated_fault_backs_off_instead_of_spinning(live, monkeypatch):
    """A bug that fires every tick should not burn a core reporting nothing."""
    app, session, instance = live
    session.follow_thread(THREAD)
    wait_until(lambda: THREAD in app.wait_for_connection(1).followed_threads())

    ticks: list[float] = []

    def boom(_older_than):
        ticks.append(time.monotonic())
        raise RuntimeError("always")

    monkeypatch.setattr(session.follow_manager(instance), "stale_awaiting", boom)
    assert wait_until(lambda: len(ticks) >= 4, timeout=20.0)

    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    # A fixed retry gives near-equal gaps, so require real growth rather than
    # whatever jitter happens to produce.
    assert gaps[-1] >= gaps[0] * 1.8, f"backoff did not grow: {gaps}"


def test_a_fault_before_the_app_is_touched_does_not_kill_the_pump(live, monkeypatch):
    """`_reap_runs` runs before anything app-facing and used to sit outside the try."""
    app, session, instance = live
    session.follow_thread(THREAD)
    wait_until(lambda: THREAD in app.wait_for_connection(1).followed_threads())

    def blow_up(_instance):
        raise RuntimeError("reaping blew up")

    monkeypatch.setattr(session, "_reap_runs", blow_up)
    assert wait_until(lambda: health_of(session, THREAD)["health"] == "lost")
    assert session._pumps["default"].is_alive()
