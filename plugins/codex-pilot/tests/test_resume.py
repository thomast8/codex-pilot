"""The detached route: `codex exec resume` for threads no app owns.

Exercised with a stub `codex` that records its argv, cwd and environment, so the
invocation contract is pinned without launching a real agent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_pilot.instances import Instance
from codex_pilot.resume import DetachedError, DetachedRunner, LockedError
from codex_pilot.threads import ThreadStore

TID = "01a039f5-2b49-7ef3-9b43-92673a44dd43"


def make_stub(tmp_path: Path, record: Path, exit_code: int = 0) -> Path:
    stub = tmp_path / "codex-stub"
    stub.write_text(
        "#!/bin/sh\n"
        f'python3 -c "\n'
        "import json,os,sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        f"'codex_home': os.environ.get('CODEX_HOME')}}, open('{record}','w'))\n"
        '" "$@"\n'
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)
    return stub


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "codexhome"
    (h / "thread-writer-locks").mkdir(parents=True)
    (h / "sessions" / "2026" / "08" / "26").mkdir(parents=True)
    (h / "archived_sessions").mkdir(parents=True)
    return h


def write_rollout(home: Path, cwd: Path, archived: bool = False) -> None:
    sub = home / ("archived_sessions" if archived else "sessions/2026/08/26")
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"rollout-2026-08-26T10-00-00-{TID}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": str(cwd), "id": TID}}) + "\n"
    )


def runner(home: Path, tmp_path: Path, record: Path, holders=None) -> DetachedRunner:
    inst = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
    store = ThreadStore(home, lock_holder_probe=lambda paths: holders or {})
    return DetachedRunner(inst, store, codex_binary=make_stub(tmp_path, record))


def test_refuses_when_a_writer_lock_is_held(home, tmp_path):
    # The app owns it; a detached run would fight for the same single writer.
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    r = runner(home, tmp_path, tmp_path / "rec.json", holders={TID: "codex(7687)"})
    with pytest.raises(LockedError):
        r.run(TID, "hello")


def test_spawns_with_the_instance_codex_home(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    rec = tmp_path / "rec.json"
    run = runner(home, tmp_path, rec).run(TID, "hello")
    run.wait(timeout=15)
    got = json.loads(rec.read_text())
    # Without this the detached turn would land in the wrong instance's store.
    assert got["codex_home"] == str(home)


def test_runs_in_the_threads_own_cwd(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    rec = tmp_path / "rec.json"
    run = runner(home, tmp_path, rec).run(TID, "hello")
    run.wait(timeout=15)
    got = json.loads(rec.read_text())
    assert Path(got["cwd"]).resolve() == work.resolve()


def test_argv_puts_global_flags_before_the_resume_subcommand(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    rec = tmp_path / "rec.json"
    run = runner(home, tmp_path, rec).run(TID, "do the thing", sandbox="read-only")
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    # `codex exec --sandbox X resume <id>` works; the flags are rejected after
    # the subcommand.
    assert argv[0] == "exec"
    assert argv.index("--sandbox") < argv.index("resume")
    assert argv[argv.index("resume") + 1] == TID
    assert argv[-1] == "do the thing"


def test_approval_policy_is_explicit(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    rec = tmp_path / "rec.json"
    run = runner(home, tmp_path, rec).run(TID, "x", approval="never")
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    # A detached run has no TTY, so an on-request policy would stall forever.
    assert "approval_policy=never" in " ".join(argv)


def test_writes_a_log_for_the_turn(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    run = runner(home, tmp_path, tmp_path / "rec.json").run(TID, "hello")
    run.wait(timeout=15)
    assert run.log_path.exists()


def test_missing_cwd_is_refused_rather_than_guessed(home, tmp_path):
    write_rollout(home, tmp_path / "gone")
    r = runner(home, tmp_path, tmp_path / "rec.json")
    with pytest.raises(DetachedError, match="cwd"):
        r.run(TID, "hello")


def test_unknown_thread_is_refused(home, tmp_path):
    r = runner(home, tmp_path, tmp_path / "rec.json")
    with pytest.raises(DetachedError):
        r.run("01a039f5-d257-7f92-80c6-315b959dec95", "hello")


def test_reports_the_archived_thread_it_unarchived(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work, archived=True)
    calls: list[list[str]] = []

    def fake_unarchive(self, thread_id: str) -> None:
        calls.append(["unarchive", thread_id])
        write_rollout(home, work, archived=False)

    monkeypatch.setattr(DetachedRunner, "_unarchive", fake_unarchive)
    run = runner(home, tmp_path, tmp_path / "rec.json").run(TID, "hello")
    run.wait(timeout=15)
    # Unarchiving is a visible side effect, so it is reported rather than silent.
    assert run.unarchived is True
    assert calls == [["unarchive", TID]]


def test_binary_defaults_to_the_instance_app_when_present(tmp_path):
    app = tmp_path / "ChatGPT.app"
    binary = app / "Contents" / "Resources" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    home = tmp_path / "h"
    (home / "thread-writer-locks").mkdir(parents=True)
    inst = Instance(slug="default", codex_home=home, app_path=app, is_default=True)
    r = DetachedRunner(inst, ThreadStore(home, lock_holder_probe=lambda p: {}))
    # The app writes the rollout store; resuming with a different build risks
    # a format mismatch.
    assert r.codex_binary == binary


def test_binary_falls_back_to_path_when_no_app_bundle(tmp_path):
    home = tmp_path / "h"
    (home / "thread-writer-locks").mkdir(parents=True)
    inst = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
    r = DetachedRunner(inst, ThreadStore(home, lock_holder_probe=lambda p: {}))
    assert r.codex_binary.name == "codex"


def test_env_is_inherited_apart_from_codex_home(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    monkeypatch.setenv("CODEX_HOME", "/somewhere/else")
    rec = tmp_path / "rec.json"
    run = runner(home, tmp_path, rec).run(TID, "hello")
    run.wait(timeout=15)
    assert json.loads(rec.read_text())["codex_home"] == str(home)
    assert os.environ["CODEX_HOME"] == "/somewhere/else"
