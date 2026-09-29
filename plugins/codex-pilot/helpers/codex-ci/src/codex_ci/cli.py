"""Run and administer the local GitHub-to-Codex event helper."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from codex_desktop_core.actions import ActionError, Session
from codex_desktop_core.ipc import IpcError
from codex_desktop_core.threads import ThreadError

from .delivery import deliver_one
from .server import serve
from .store import Store

DEFAULT_DB = Path.home() / "Library/Application Support/codex-ci/events.sqlite3"


def _target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repo", help="GitHub owner/repository")
    parser.add_argument("pr", type=int, help="pull request number")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-ci")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind", help="bind one PR to an existing Codex task")
    _target(bind)
    bind.add_argument("--instance", required=True, help="Codex instance slug")
    bind.add_argument("--thread", required=True, help="exact Codex task UUID")
    retry = sub.add_parser("retry", help="explicitly retry held or interrupted deliveries")
    _target(retry)
    sub.add_parser("status", help="show bindings and delivery counts")
    sub.add_parser("drain", help="attempt pending events now")
    listener = sub.add_parser("serve", help="receive signed GitHub webhooks")
    listener.add_argument("--host", default="127.0.0.1")
    listener.add_argument("--port", type=int, default=8765)
    listener.add_argument("--secret-env", default="CODEX_CI_WEBHOOK_SECRET")
    listener.add_argument(
        "--ignore-user", action="append", default=[],
        help="ignore review events from this GitHub login (repeatable)",
    )
    return parser


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.db)
    if args.command == "status":
        _print({"bindings": store.bindings(), "events": store.counts()})
        return 0
    if args.command == "bind":
        if args.pr <= 0 or args.repo.count("/") != 1:
            raise SystemExit("bind needs OWNER/REPO and a positive PR number")
        session = Session()
        try:
            resolved = session.resolve(args.thread, args.instance)
            store.bind(args.repo, args.pr, resolved.instance.slug, resolved.thread_id)
        except (ActionError, IpcError, ThreadError) as exc:
            raise SystemExit(str(exc)) from exc
        finally:
            session.close()
        _print(
            {"status": "bound", "repo": args.repo, "pr": args.pr,
             "instance": args.instance, "thread": args.thread}
        )
        return 0
    if args.command == "retry":
        _print({"status": "pending", "count": store.retry(args.repo, args.pr)})
        return 0
    if args.command == "drain":
        session = Session()
        try:
            while (result := deliver_one(store, session)) is not None:
                _print(result)
        finally:
            session.close()
        return 0
    secret = os.environ.get(args.secret_env)
    if not secret:
        raise SystemExit(f"{args.secret_env} must contain the GitHub webhook secret")
    serve(
        store, secret, args.host, args.port,
        ignored_users=frozenset(user.lower() for user in args.ignore_user),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
