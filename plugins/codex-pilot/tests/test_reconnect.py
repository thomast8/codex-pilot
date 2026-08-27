"""A replaced IPC connection has to re-subscribe every followed thread.

The app tracks subscriptions per connection, so a reconnect leaves every follow
silently dead: no frames arrive, no error is raised, and `collect_events` keeps
reporting the thread as followed. These tests drive a real `Session` against a
real socket and assert on what the *second* connection was told, because our own
bookkeeping looking right is exactly the failure mode.
"""

from __future__ import annotations

import contextlib
import tempfile
import threading
import time
from pathlib import Path

import pytest

from codex_pilot.actions import Session
from codex_pilot.follow import EVENT_RESYNC
from codex_pilot.instances import Instance
from fakeapp import FakeApp, wait_until, write_index

THREAD = "01a03e2b-a106-77a3-add2-913ac3f7336a"


@pytest.fixture
def live():
    """A fake app plus a Session bound to it, in a short-enough socket dir."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        app = FakeApp(home)
        instance = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
        session = Session(instances=[instance])
        try:
            yield app, session
        finally:
            session.close()
            app.close()


def test_the_first_connection_is_told_about_the_follow(live):
    app, session = live
    session.follow_thread(THREAD)

    first = app.wait_for_connection(1)
    assert wait_until(lambda: THREAD in first.followed_threads())


def test_a_replaced_connection_resubscribes_the_followed_thread(live):
    app, session = live
    session.follow_thread(THREAD)
    first = app.wait_for_connection(1)
    assert wait_until(lambda: THREAD in first.followed_threads())

    first.close()

    second = app.wait_for_connection(2)
    assert second is not first
    assert wait_until(lambda: THREAD in second.followed_threads()), (
        "the new connection was never told to follow the thread, so the app "
        "sends nothing and the follow is silently dead"
    )


def test_a_drop_before_the_pump_first_ticks_still_resubscribes(live):
    """`follow_thread` subscribes on one client; if that client dies before the
    pump ever runs, the pump's *first* client is already a replacement. Nothing
    the pump can observe about its own first connection reveals that, which is
    why the reconnect has to be noticed where clients are created."""
    app, session = live
    session.follow_thread(THREAD)
    first = app.wait_for_connection(1)
    first.close()  # deliberately not waiting for the pump to tick

    second = app.wait_for_connection(2)
    assert wait_until(lambda: THREAD in second.followed_threads()), (
        "a follow subscribed just before the connection died was never restored"
    )


def test_a_reconnect_emits_an_event_so_silence_is_never_ambiguous(live):
    app, session = live
    session.follow_thread(THREAD)
    first = app.wait_for_connection(1)
    assert wait_until(lambda: THREAD in first.followed_threads())

    first.close()
    app.wait_for_connection(2)

    def saw_gap() -> bool:
        got = session.collect_events(wait_seconds=0.0)
        return any(e["type"] == EVENT_RESYNC for e in got["events"])

    assert wait_until(saw_gap), "a gap in the stream must surface as an event"


def test_an_app_that_goes_away_and_returns_resubscribes(live):
    """The common case: Codex Desktop restarts, so there is no client at all
    for a while. Coming back must not look like a first connection."""
    app, session = live
    home = app.path.parent.parent
    session.follow_thread(THREAD)
    first = app.wait_for_connection(1)
    assert wait_until(lambda: THREAD in first.followed_threads())

    app.close()
    # The pump reports the app as unreachable while nothing is listening.
    assert wait_until(
        lambda: any(
            e["type"] == "follow_lost" for e in session.collect_events(wait_seconds=0.0)["events"]
        ),
        timeout=20.0,
    )

    restarted = FakeApp(home)
    try:
        conn = restarted.wait_for_connection(1, timeout=30.0)
        assert wait_until(lambda: THREAD in conn.followed_threads(), timeout=30.0), (
            "a restarted app was never told to follow the thread, so the watch "
            "stays silent for the rest of its life"
        )
    finally:
        restarted.close()


def _resync_count(session) -> int:
    """Every resync still buffered. `after=0` so the count never resets."""
    events = session.collect_events(wait_seconds=0.0)["events"]
    return len([e for e in events if e["type"] == EVENT_RESYNC])


def test_a_healthy_connection_does_not_emit_repeated_resyncs(live):
    """The reconnect check keys on client identity, not on every pump tick."""
    app, session = live
    session.follow_thread(THREAD)
    app.wait_for_connection(1)

    # Polls across several pump ticks (the loop sleeps 0.5s between iterations).
    assert not wait_until(lambda: _resync_count(session) > 0, timeout=3.0)


def test_one_reconnect_produces_exactly_one_resync(live):
    """Dropping the `seen = client` reassignment would re-fire every tick and
    bury real events under resync spam."""
    app, session = live
    session.follow_thread(THREAD)
    first = app.wait_for_connection(1)
    assert wait_until(lambda: THREAD in first.followed_threads())

    first.close()
    app.wait_for_connection(2)

    assert wait_until(lambda: _resync_count(session) >= 1)
    assert not wait_until(lambda: _resync_count(session) > 1, timeout=3.0)


def test_a_stalled_handshake_does_not_block_other_session_work():
    """Opening a connection must not hold the session guard across the IPC
    handshake. An app that accepts and then says nothing would otherwise freeze
    every unrelated caller -- thread stores, follow managers, collect_events --
    for the full 15s IPC timeout.
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        app = FakeApp(home, answer_initialize=False)
        instance = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
        session = Session(instances=[instance])
        try:

            def connect() -> None:
                with contextlib.suppress(Exception):
                    session.client(instance)

            stuck = threading.Thread(target=connect, name="stuck-handshake", daemon=True)
            stuck.start()
            app.wait_for_connection(1)  # the handshake is in flight and never answered

            started = time.monotonic()
            session.collect_events(wait_seconds=0.0)
            waited = time.monotonic() - started

            assert waited < 3.0, (
                f"an unrelated caller waited {waited:.1f}s behind a stalled handshake"
            )
        finally:
            session.close()
            app.close()
