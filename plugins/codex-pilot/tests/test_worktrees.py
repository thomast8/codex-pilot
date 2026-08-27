"""Making a worktree the way Codex makes one.

Exercised against real git repositories in tmp_path rather than a stub: the
whole point of this module is that git actually registers the worktree and puts
it on a branch, which a fake would not tell us.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_pilot import worktrees
from codex_pilot.worktrees import WorktreeError


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "myrepo"
    path.mkdir()
    git(["init", "-q"], path)
    (path / "a.txt").write_text("x\n")
    git(["add", "-A"], path)
    git(["commit", "-qm", "init"], path)
    return path


def test_creates_a_registered_worktree_on_a_new_branch(repo, tmp_path):
    wt = worktrees.create(repo, "feature/slice", root=tmp_path / "root")
    assert wt.path.is_dir()
    assert (wt.path / "a.txt").exists()
    # A real worktree, not just a directory: git knows about it and it is on a
    # branch, unlike the detached-HEAD directory a hand-rolled attempt produces.
    listed = git(["worktree", "list"], repo).stdout
    assert str(wt.path) in listed
    head = git(["rev-parse", "--abbrev-ref", "HEAD"], wt.path).stdout.strip()
    assert head == "feature/slice"


def test_follows_the_codex_layout(repo, tmp_path):
    root = tmp_path / "root"
    wt = worktrees.create(repo, "feature/slice", root=root)
    # <root>/<short hex>/<repo name>, which is where Codex puts its own.
    assert wt.path.parent.parent == root
    assert wt.path.name == "myrepo"
    assert len(wt.path.parent.name) == worktrees.SLUG_LENGTH


def test_default_root_is_the_instances_own_worktree_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(worktrees.ROOT_ENV, raising=False)
    assert worktrees.default_root(tmp_path / ".codex") == tmp_path / ".codex" / "worktrees"
    monkeypatch.setenv(worktrees.ROOT_ENV, str(tmp_path / "elsewhere"))
    # Settings > Git keeps the real root inside the app, so an override is
    # honoured rather than guessed.
    assert worktrees.default_root(tmp_path / ".codex") == tmp_path / "elsewhere"


def test_an_existing_branch_is_refused_rather_than_reused(repo, tmp_path):
    git(["branch", "feature/taken"], repo)
    with pytest.raises(WorktreeError, match="already exists"):
        worktrees.create(repo, "feature/taken", root=tmp_path / "root")


def test_an_illegal_branch_name_is_refused(repo, tmp_path):
    with pytest.raises(WorktreeError, match="not a valid git branch name"):
        worktrees.create(repo, "bad branch~name", root=tmp_path / "root")


def test_a_directory_outside_a_repo_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not inside a git repository"):
        worktrees.create(plain, "feature/x", root=tmp_path / "root")


def test_a_house_branch_pattern_is_enforced_when_set(repo, tmp_path, monkeypatch):
    monkeypatch.setenv(worktrees.BRANCH_PATTERN_ENV, r"^(feat|feature|fix)/[a-z0-9._-]+$")
    with pytest.raises(WorktreeError, match="does not match"):
        worktrees.create(repo, "wip/whatever", root=tmp_path / "root")
    ok = worktrees.create(repo, "fix/a-thing", root=tmp_path / "root")
    assert ok.branch == "fix/a-thing"


def test_base_selects_what_the_branch_forks_from(repo, tmp_path):
    first = git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / "b.txt").write_text("y\n")
    git(["add", "-A"], repo)
    git(["commit", "-qm", "second"], repo)
    wt = worktrees.create(repo, "feature/from-base", root=tmp_path / "root", base=first)
    assert git(["rev-parse", "HEAD"], wt.path).stdout.strip() == first
    assert not (wt.path / "b.txt").exists()


def test_a_subdirectory_resolves_to_the_repo_root(repo, tmp_path):
    sub = repo / "nested" / "deep"
    sub.mkdir(parents=True)
    wt = worktrees.create(sub, "feature/from-sub", root=tmp_path / "root")
    assert wt.repo == repo.resolve()
