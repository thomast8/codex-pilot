"""Making a git worktree the way Codex Desktop makes one.

Codex puts a thread's worktree at `<root>/<short hex>/<repo name>`, where the
root is `$CODEX_HOME/worktrees` unless changed in Settings > Git, and it is a
real registered worktree of the origin repo on its own branch -- the `.git` file
points back at `<repo>/.git/worktrees/<name>`. Following that layout is
deliberate: worktrees for one machine end up in one place the user already knows
and already browses, rather than in a second convention invented here.

Two consequences of following it, both worth knowing before you put work there:

**Codex may clean this root up.** The app removes worktrees under it to reclaim
space, and tells you so after the fact ("This chat's worktree was removed to save
space"). So a worktree here is a workspace, not storage: commit and push the
branch, do not park unmerged work in it.

**The configured root cannot be read back.** Settings > Git stores it inside the
app rather than in `config.toml`, so an override is honoured through
`CODEX_PILOT_WORKTREE_ROOT` rather than guessed.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

WORKTREE_DIR_NAME = "worktrees"
ROOT_ENV = "CODEX_PILOT_WORKTREE_ROOT"
BRANCH_PATTERN_ENV = "CODEX_PILOT_BRANCH_PATTERN"
SLUG_LENGTH = 4
MAX_SLUG_ATTEMPTS = 20


class WorktreeError(Exception):
    """A worktree could not be prepared."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    repo: Path
    created: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "branch": self.branch,
            "repo": str(self.repo),
            "created": self.created,
        }


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


def default_root(codex_home: Path) -> Path:
    """Codex's own worktree root for this instance, or an explicit override."""
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return codex_home / WORKTREE_DIR_NAME


def repo_root(path: Path) -> Path:
    """The top level of the repo containing `path`."""
    if not path.is_dir():
        raise WorktreeError(f"repo {str(path)!r} is not a directory")
    result = _git(["rev-parse", "--show-toplevel"], cwd=path)
    if result.returncode != 0:
        raise WorktreeError(f"{str(path)!r} is not inside a git repository")
    return Path(result.stdout.strip())


def check_branch(name: str, repo: Path) -> None:
    """Refuse a branch name git would reject, or one that already exists.

    Reusing an existing branch is refused rather than silently attached to: an
    agent set loose on a branch that already carries unrelated work is very hard
    to untangle afterwards.
    """
    if not name or name != name.strip():
        raise WorktreeError("branch name is empty or padded with whitespace")
    if _git(["check-ref-format", "--branch", name]).returncode != 0:
        raise WorktreeError(f"{name!r} is not a valid git branch name")

    pattern = os.environ.get(BRANCH_PATTERN_ENV)
    if pattern:
        import re

        if not re.match(pattern, name):
            raise WorktreeError(f"branch {name!r} does not match {BRANCH_PATTERN_ENV} ({pattern})")

    exists = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=repo)
    if exists.returncode == 0:
        raise WorktreeError(
            f"branch {name!r} already exists; pick a new one rather than starting an "
            "agent on a branch that may already carry unrelated work"
        )


def create(
    repo: Path,
    branch: str,
    root: Path,
    base: str | None = None,
) -> Worktree:
    """Add a worktree on a new branch, laid out the way Codex lays one out."""
    top = repo_root(repo)
    check_branch(branch, top)

    root.mkdir(parents=True, exist_ok=True)
    target: Path | None = None
    for _ in range(MAX_SLUG_ATTEMPTS):
        candidate = root / uuid.uuid4().hex[:SLUG_LENGTH] / top.name
        if not candidate.exists():
            target = candidate
            break
    if target is None:
        raise WorktreeError(f"could not find a free slug under {root}")

    args = ["worktree", "add", "-b", branch, str(target)]
    if base:
        args.append(base)
    result = _git(args, cwd=top)
    if result.returncode != 0:
        raise WorktreeError(
            f"could not create a worktree for {branch!r}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return Worktree(path=target, branch=branch, repo=top, created=True)
