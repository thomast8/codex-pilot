"""The `codex-pilot watch` CLI, driven as a real subprocess.

The whole point of the CLI is to be backgrounded by a shell, so these tests run
it the way a shell would: a separate process, a real socket, and the captured
protocol frames replayed at it. Asserting on stdout matters more than usual --
stdout is the event stream a `Monitor` turns into notifications, so anything
that lands on stderr instead is invisible to the caller.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from fakeapp import FakeApp, wait_until, write_index

THREAD = "01a03e2b-a106-77a3-add2-913ac3f7336a"
FIXTURE = Path(__file__).parent / "fixtures" / "stream_frames.json"


@pytest.fixture
def frames() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def launch(home: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "codex_pilot.cli", "watch", *args, "--codex-home", str(home)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CODEX_HOME": str(home)},
    )


def emitted(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_watch_streams_turn_completed_and_exits_zero(frames):
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        app = FakeApp(home)
        proc = launch(home, THREAD, "--until", "turn_completed", "--timeout", "60")
        try:
            conn = app.wait_for_connection(1)
            assert wait_until(lambda: THREAD in conn.followed_threads())
            app.replay(frames, conn)
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            app.close()

    events = emitted(stdout)
    assert proc.returncode == 0, f"stderr: {stderr}"
    assert "turn_completed" in [e["type"] for e in events]
    # Every line is a whole JSON object, or a Monitor filter cannot match on it.
    assert all("type" in e for e in events)


def test_watch_reports_a_timeout_on_stdout_and_exits_three():
    """Not exit 2: argparse owns that, and a usage error must not look like a
    thread that simply never finished."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        app = FakeApp(home)
        proc = launch(home, THREAD, "--until", "turn_completed", "--timeout", "2")
        try:
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            app.close()

    assert proc.returncode == 3, f"stderr: {stderr}"
    # Silence must never be indistinguishable from "nothing happened".
    assert "watch_timeout" in [e["type"] for e in emitted(stdout)]


def test_watch_without_until_treats_the_timeout_as_a_clean_end():
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        app = FakeApp(home)
        proc = launch(home, THREAD, "--timeout", "2")
        try:
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            app.close()

    assert proc.returncode == 0, f"stderr: {stderr}"
    # Exiting 0 in silence would be the failure this command exists to prevent,
    # so the clean end still has to announce itself.
    assert "watch_timeout" in [e["type"] for e in emitted(stdout)]


def test_a_usage_error_still_prints_a_line_on_stdout():
    """argparse reports on stderr, which a stdout-reading watcher never sees."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        proc = launch(home, THREAD, "--timeout", "not-a-number")
        stdout, _ = proc.communicate(timeout=60)

    assert proc.returncode == 2
    assert [e["type"] for e in emitted(stdout)] == ["watch_error"]


def test_an_unmatchable_until_event_is_rejected_immediately():
    """`--until watch_dropped` could never fire: the CLI's own lines are not
    events. Failing at parse time beats waiting out the timeout."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        proc = launch(home, THREAD, "--until", "watch_dropped", "--timeout", "2")
        stdout, _ = proc.communicate(timeout=60)

    assert proc.returncode == 2
    assert [e["type"] for e in emitted(stdout)] == ["watch_error"]


def test_watching_several_threads_announces_each_one(frames):
    other = "01a03e2b-a106-77a3-add2-000000000002"
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        home.mkdir(parents=True, exist_ok=True)
        rows = [
            json.dumps({"id": THREAD, "thread_name": "watched"}),
            json.dumps({"id": other, "thread_name": "other"}),
        ]
        (home / "session_index.jsonl").write_text("\n".join(rows) + "\n")
        app = FakeApp(home)
        proc = launch(home, THREAD, other, "--timeout", "3")
        try:
            conn = app.wait_for_connection(1)
            assert wait_until(lambda: {THREAD, other} <= set(conn.followed_threads()))
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            app.close()

    started = [e for e in emitted(stdout) if e["type"] == "watch_started"]
    assert proc.returncode == 0, f"stderr: {stderr}"
    assert {e["thread"] for e in started} == {THREAD, other}


def test_sigterm_stops_the_watch_with_a_line_and_a_signal_exit_code():
    """A harness stops a background process with SIGTERM, so dying mutely there
    would defeat the whole point of being backgroundable."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        app = FakeApp(home)
        proc = launch(home, THREAD)  # no --timeout: runs until told to stop
        try:
            conn = app.wait_for_connection(1)
            assert wait_until(lambda: THREAD in conn.followed_threads())
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            app.close()

    assert proc.returncode == 143, f"stderr: {stderr}"
    stopped = [e for e in emitted(stdout) if e["type"] == "watch_stopped"]
    assert stopped and stopped[0]["signal"] == "SIGTERM"


def test_an_unresolvable_thread_is_reported_on_stdout_and_exits_one():
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        proc = launch(home, "no-such-thread", "--timeout", "5")
        try:
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()

    assert proc.returncode == 1, f"stderr: {stderr}"
    errors = [e for e in emitted(stdout) if e["type"] == "watch_error"]
    assert errors and "no-such-thread" in errors[0]["message"]


def test_watch_surfaces_a_reconnect_rather_than_going_quiet(frames):
    """A dropped connection must produce a line, not silence."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        write_index(home, THREAD)
        app = FakeApp(home)
        proc = launch(home, THREAD, "--until", "turn_completed", "--timeout", "60")
        try:
            first = app.wait_for_connection(1)
            assert wait_until(lambda: THREAD in first.followed_threads())
            first.close()

            second = app.wait_for_connection(2)
            assert wait_until(lambda: THREAD in second.followed_threads())
            app.replay(frames, second)
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            app.close()

    types = [e["type"] for e in emitted(stdout)]
    assert proc.returncode == 0, f"stderr: {stderr}"
    assert "resync" in types, "the gap must be visible to the shell"
    assert "turn_completed" in types, "the watch must still work after reconnecting"
