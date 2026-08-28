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


APP_PID = 78222
FOREIGN_PID = 69843


def runner(
    home: Path,
    tmp_path: Path,
    record: Path,
    holders=None,
    app_pids: frozenset[int] | None = frozenset({APP_PID}),
    lock_probe=None,
) -> DetachedRunner:
    inst = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
    store = ThreadStore(
        home,
        lock_holder_probe=lock_probe or (lambda paths: dict(holders or {})),
        app_process_probe=lambda socks: None if app_pids is None else set(app_pids),
    )
    return DetachedRunner(inst, store, codex_binary=make_stub(tmp_path, record))


def test_refuses_when_the_app_holds_the_writer_lock(home, tmp_path):
    # The app owns it; a detached run would fight for the same single writer.
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    r = runner(home, tmp_path, tmp_path / "rec.json", holders={TID: (APP_PID, "codex")})
    with pytest.raises(LockedError):
        r.run(TID, "hello")


def test_refuses_when_a_writer_that_is_not_the_app_holds_the_lock(home, tmp_path):
    # The dangerous regression: once `app_owned` was narrowed to mean "the app
    # specifically", a check written against it would wave a foreign writer
    # through and put a second `codex exec` on the same rollout. The refusal
    # has to key on the lock being held at all, not on who is holding it.
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    r = runner(home, tmp_path, tmp_path / "rec.json", holders={TID: (FOREIGN_PID, "codex")})
    with pytest.raises(LockedError):
        r.run(TID, "hello")


def test_refuses_when_the_holder_could_not_be_classified(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    r = runner(
        home,
        tmp_path,
        tmp_path / "rec.json",
        holders={TID: (FOREIGN_PID, "codex")},
        app_pids=None,
    )
    with pytest.raises(LockedError):
        r.run(TID, "hello")


def test_refuses_when_the_lock_state_could_not_be_probed(home, tmp_path):
    # A probe that could not run has not established that the lock is free.
    # Reading its silence as "nothing holds it" is how a second writer lands
    # on a rollout the app is already writing.
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    r = runner(home, tmp_path, tmp_path / "rec.json", lock_probe=lambda paths: None)
    with pytest.raises(LockedError, match="could not be"):
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
    r = DetachedRunner(
        inst,
        ThreadStore(
            home,
            lock_holder_probe=lambda p: {},
            app_process_probe=lambda socks: set(),
        ),
    )
    # The app writes the rollout store; resuming with a different build risks
    # a format mismatch.
    assert r.codex_binary == binary


def test_binary_falls_back_to_path_when_no_app_bundle(tmp_path):
    home = tmp_path / "h"
    (home / "thread-writer-locks").mkdir(parents=True)
    inst = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
    r = DetachedRunner(
        inst,
        ThreadStore(
            home,
            lock_holder_probe=lambda p: {},
            app_process_probe=lambda socks: set(),
        ),
    )
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


# -- creating a new thread ----------------------------------------------------


def json_stub(tmp_path: Path, record: Path, lines: list[str], exit_code: int = 0) -> Path:
    """A stub that records its invocation and emits `lines` on stdout as the CLI would."""
    stub = tmp_path / "codex-json-stub"
    emit = "\n".join(f"printf '%s\\n' '{line}'" for line in lines)
    stub.write_text(
        "#!/bin/sh\n"
        f'python3 -c "\n'
        "import json,os,sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        f"'codex_home': os.environ.get('CODEX_HOME')}}, open('{record}','w'))\n"
        '" "$@"\n'
        f"{emit}\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)
    return stub


STARTED = '{"type": "thread.started", "thread_id": "01a03f10-e3e1-7b30-9dfc-7c659c4d7434"}'
NEW_TID = "01a03f10-e3e1-7b30-9dfc-7c659c4d7434"


def new_runner(home: Path, tmp_path: Path, record: Path, lines: list[str]) -> DetachedRunner:
    inst = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
    store = ThreadStore(
        home,
        lock_holder_probe=lambda paths: {},
        app_process_probe=lambda socks: set(),
    )
    return DetachedRunner(inst, store, codex_binary=json_stub(tmp_path, record, lines))


def test_start_reports_the_new_thread_id(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, [STARTED]).start("build it", cwd=work)
    run.wait(timeout=15)
    # Without the id the caller cannot follow, steer or harvest the thread.
    assert run.thread_id == NEW_TID


def test_start_runs_in_the_directory_it_was_given(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, [STARTED]).start("build it", cwd=work)
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    # The whole point: a new thread must not inherit the caller's cwd.
    assert argv[argv.index("--cd") + 1] == str(work)
    assert Path(json.loads(rec.read_text())["cwd"]).resolve() == work.resolve()


def test_start_asks_for_json_so_the_id_can_be_read(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, [STARTED]).start("x", cwd=work)
    run.wait(timeout=15)
    assert "--json" in json.loads(rec.read_text())["argv"]


def test_start_refuses_a_directory_that_does_not_exist(home, tmp_path):
    rec = tmp_path / "rec.json"
    r = new_runner(home, tmp_path, rec, [STARTED])
    with pytest.raises(DetachedError, match="not a directory"):
        r.start("x", cwd=tmp_path / "nope")


def test_start_survives_a_run_that_never_reports_an_id(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, ["boom: could not start"]).start(
        "x", cwd=work, wait_for_id=2.0
    )
    run.wait(timeout=15)
    # A failed start must still hand back the log rather than raising blind.
    assert run.thread_id is None
    assert run.log_path.exists()


def test_start_skips_non_json_preamble_on_stdout(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    lines = ["Reading additional input from stdin...", STARTED]
    run = new_runner(home, tmp_path, rec, lines).start("x", cwd=work)
    run.wait(timeout=15)
    # The real CLI prints a human line before the JSONL stream.
    assert run.thread_id == NEW_TID


def test_start_passes_model_through(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, [STARTED]).start("x", cwd=work, model="gpt-5.4-codex")
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-5.4-codex"


def test_start_ignores_a_decoy_id_on_an_earlier_event(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    # A real `codex exec --json` stream carries ids on later events too, so
    # matching "first JSON object with a thread_id" would take the wrong one.
    decoy = '{"type": "turn.started", "thread_id": "00000000-dead-beef-0000-000000000000"}'
    run = new_runner(home, tmp_path, rec, [decoy, STARTED]).start("x", cwd=work)
    run.wait(timeout=15)
    assert run.thread_id == NEW_TID


def test_start_gives_up_on_a_run_that_hangs_without_reporting(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    # Still alive and silent: the deadline is the only thing that ends this,
    # unlike a stub that exits and returns via the process-exited branch.
    runner_ = new_runner(home, tmp_path, rec, ["still working..."])
    stub = runner_.codex_binary
    stub.write_text(stub.read_text().replace("exit 0", "sleep 30\nexit 0"))
    run = runner_.start("x", cwd=work, wait_for_id=0.3)
    try:
        assert run.thread_id is None
        assert run.running is True
    finally:
        run.process.terminate()
        run.wait(timeout=15)


def test_start_puts_the_prompt_after_a_double_dash(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, [STARTED]).start("--help", cwd=work)
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    # Without `--` the CLI parses a leading-dash prompt as a flag and the turn
    # never runs.
    assert argv[-2:] == ["--", "--help"]


def test_resume_also_puts_the_prompt_after_a_double_dash(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    rec = tmp_path / "rec.json"
    run = runner(home, tmp_path, rec).run(TID, "--version")
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    assert argv[-3:] == [TID, "--", "--version"]


def test_a_bare_dash_prompt_is_refused(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    r = new_runner(home, tmp_path, tmp_path / "rec.json", [STARTED])
    # `-` means "read the prompt from stdin", and stdin is DEVNULL here, so it
    # would start a thread with no prompt at all.
    with pytest.raises(DetachedError, match="stdin"):
        r.start("-", cwd=work)


def test_sandbox_and_approval_reach_the_cli_as_given(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, [STARTED]).start(
        "x", cwd=work, sandbox="read-only", approval="on-request"
    )
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "approval_policy=on-request" in argv


def test_an_unknown_sandbox_is_refused_before_spawning(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    r = new_runner(home, tmp_path, tmp_path / "rec.json", [STARTED])
    with pytest.raises(DetachedError, match="sandbox must be one of"):
        r.start("x", cwd=work, sandbox="workspace-write --dangerously-bypass-approvals-and-sandbox")


def test_full_access_needs_an_explicit_env_opt_in(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    r = new_runner(home, tmp_path, rec, [STARTED])
    monkeypatch.delenv("CODEX_PILOT_ALLOW_FULL_ACCESS", raising=False)
    with pytest.raises(DetachedError, match="danger-full-access"):
        r.start("x", cwd=work, sandbox="danger-full-access")
    monkeypatch.setenv("CODEX_PILOT_ALLOW_FULL_ACCESS", "1")
    run = r.start("x", cwd=work, sandbox="danger-full-access")
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"


# -- effort and service tier, per dispatch ------------------------------------
#
# Both are otherwise inherited from `~/.codex/config.toml`, which Codex Desktop
# owns and rewrites. Without a per-call lever the only way to dispatch at a
# chosen effort is to edit that shared file, which races the app and changes
# every other thread and interactive session too.


def test_start_passes_effort_and_service_tier_as_config_overrides(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, [STARTED]).start(
        "x", cwd=work, effort="xhigh", service_tier="priority"
    )
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    assert "model_reasoning_effort=xhigh" in argv
    assert "service_tier=priority" in argv


def test_resume_passes_effort_and_service_tier_as_config_overrides(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work)
    rec = tmp_path / "rec.json"
    # Every resume is a fresh process, so a follow-up turn needs the flags
    # again -- the previous run's settings do not carry over.
    run = runner(home, tmp_path, rec).run(TID, "x", effort="max", service_tier="flex")
    run.wait(timeout=15)
    argv = json.loads(rec.read_text())["argv"]
    assert "model_reasoning_effort=max" in argv
    assert "service_tier=flex" in argv
    assert argv.index("-c") < argv.index("resume")


def test_neither_is_sent_when_neither_was_asked_for(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    run = new_runner(home, tmp_path, rec, [STARTED]).start("x", cwd=work)
    run.wait(timeout=15)
    argv = " ".join(json.loads(rec.read_text())["argv"])
    # No default of our own: an unasked-for dispatch keeps whatever the
    # instance is configured for, rather than a rung invented here.
    assert "model_reasoning_effort" not in argv
    assert "service_tier" not in argv


def test_an_effort_rung_this_code_has_never_heard_of_still_reaches_the_cli(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    rec = tmp_path / "rec.json"
    # The ladder is per model and comes from a server-side catalogue, so an
    # allowlist here would refuse a rung a newer model added.
    run = new_runner(home, tmp_path, rec, [STARTED]).start("x", cwd=work, effort="ultra2")
    run.wait(timeout=15)
    assert "model_reasoning_effort=ultra2" in json.loads(rec.read_text())["argv"]


@pytest.mark.parametrize(
    "effort",
    ['xhigh"\nmodel="gpt-4', "xhigh sandbox_mode=danger-full-access", "", "x high"],
)
def test_an_effort_that_is_not_a_bare_token_is_refused(home, tmp_path, effort):
    work = tmp_path / "work"
    work.mkdir()
    r = new_runner(home, tmp_path, tmp_path / "rec.json", [STARTED])
    # `-c` parses the right-hand side as TOML, so anything but a bare token is
    # a way to smuggle a second setting past the allowlists above it.
    with pytest.raises(DetachedError, match="effort"):
        r.start("x", cwd=work, effort=effort)


def test_an_unknown_service_tier_is_refused_before_spawning(home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    r = new_runner(home, tmp_path, tmp_path / "rec.json", [STARTED])
    with pytest.raises(DetachedError, match="service_tier must be one of"):
        r.start("x", cwd=work, service_tier="fast")


def test_a_resume_checks_both_before_it_unarchives_anything(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    write_rollout(home, work, archived=True)
    r = runner(home, tmp_path, tmp_path / "rec.json")
    unarchived = []
    monkeypatch.setattr(r, "_unarchive", lambda tid: unarchived.append(tid))
    with pytest.raises(DetachedError, match="service_tier must be one of"):
        r.run(TID, "x", service_tier="fast")
    # Refusing after unarchiving would leave the thread moved for a turn that
    # never ran.
    assert unarchived == []
