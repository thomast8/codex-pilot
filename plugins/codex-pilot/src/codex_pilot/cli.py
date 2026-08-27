"""Shell entry point, so waiting for a thread does not cost an agent turn.

MCP has no server-initiated push, so `collect_events(wait_seconds=...)` can only
wait by blocking the call -- and a blocked tool call freezes the whole turn that
made it. A Claude Code session cannot check another thread, react, or be
interrupted cleanly while one is outstanding.

A shell command has none of that problem, because the harness already knows how
to background one. `codex-pilot watch` is that command:

    # one notification when the thread goes idle
    codex-pilot watch <thread> --until turn_completed --timeout 900

    # a line per event, for a streaming watch
    codex-pilot watch <thread-a> <thread-b>

Events go to stdout as one JSON object per line, flushed immediately, because
that is what a line-oriented watcher can filter on. Every way this can end
prints a line first -- timeout, error, bad arguments, a signal -- so a watcher
that has died is never mistaken for a thread that is merely quiet. Note that
`resync`, `follow_lost` and `watch_dropped` report trouble without ending the
watch; only `watch_timeout`, `watch_error`, a `--until` match, or a signal do.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import sys
import time
from pathlib import Path
from types import FrameType
from typing import Any

from .actions import ActionError, Session
from .follow import (
    EVENT_FOLLOW_LOST,
    EVENT_REQUEST_PENDING,
    EVENT_REQUEST_RESOLVED,
    EVENT_RESYNC,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_STARTED,
)
from .instances import Instance
from .ipc import IpcError
from .threads import ThreadError

EXIT_OK = 0
EXIT_ERROR = 1
# argparse owns 2 for usage errors and cannot be talked out of it, so a
# semantic timeout has to live somewhere else -- otherwise "you typed the
# command wrong" and "the thread never did the thing" are the same answer.
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_INTERRUPTED = 130

# What `--until` can actually wait for: the events the follow subsystem
# produces. The CLI's own `watch_*` lines never enter the event batch, so
# accepting one here would wait forever for something that cannot arrive.
MATCHABLE_EVENTS = (
    EVENT_TURN_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_REQUEST_PENDING,
    EVENT_REQUEST_RESOLVED,
    EVENT_RESYNC,
    EVENT_FOLLOW_LOST,
)

EXIT_CODES = f"""exit codes:
  {EXIT_OK}    the --until event arrived, or a watch with no --until reached its timeout
  {EXIT_ERROR}    error (thread could not be resolved, app unreachable)
  {EXIT_USAGE}    bad arguments
  {EXIT_TIMEOUT}    timed out before the requested --until event arrived
  {EXIT_INTERRUPTED}  interrupted (SIGINT; SIGTERM exits 143)

Every exit prints a JSON line on stdout first, so silence always means the
watch is still running."""

# Long enough that an idle watch is nearly free, short enough that a timeout is
# honoured promptly. Internal: the caller expresses intent with --timeout.
POLL_SECONDS = 2.0


class Terminated(Exception):
    """A signal asked us to stop. Carries the number so the exit code can say which."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


def emit(payload: dict[str, Any]) -> None:
    """One JSON object per line, flushed, because a pipe buffer hides events."""
    print(json.dumps(payload), flush=True)


def install_signal_handlers() -> None:
    """Turn SIGTERM into an exception so the unfollow/close cleanup still runs.

    The whole point of this command is to be backgrounded, and a harness stops a
    background process with SIGTERM, not SIGINT. Left at the default handler
    that is a silent immediate death: no final line, and the follow left
    registered on the far side.
    """

    def handler(signum: int, _frame: FrameType | None) -> None:
        raise Terminated(signum)

    # signal() only works on the main thread; a caller embedding this elsewhere
    # keeps the default behaviour rather than crashing.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, handler)


def build_session(codex_home: Path | None) -> Session:
    """Pin a single instance when asked, otherwise discover them all."""
    if codex_home is None:
        return Session()
    instance = Instance(slug="default", codex_home=codex_home, app_path=None, is_default=True)
    return Session(instances=[instance])


def watch(args: argparse.Namespace) -> int:
    install_signal_handlers()
    session = build_session(Path(args.codex_home) if args.codex_home else None)
    followed: list[tuple[str, str]] = []
    until = set(args.until or [])
    try:
        try:
            for ref in args.thread:
                result = session.follow_thread(ref, follow=True, instance=args.instance)
                followed.append((result["thread"], result["instance"]))
                emit(
                    {
                        "type": "watch_started",
                        "thread": result["thread"],
                        "instance": result["instance"],
                        "name": result["name"],
                    }
                )
        except (ActionError, IpcError, ThreadError) as exc:
            emit({"type": "watch_error", "error": type(exc).__name__, "message": str(exc)})
            return EXIT_ERROR

        deadline = time.monotonic() + args.timeout if args.timeout > 0 else None
        cursor = 0
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                emit({"type": "watch_timeout", "waited_seconds": args.timeout})
                # A watch that was asked for a specific event and never saw it
                # did not do its job; one that was only streaming did.
                return EXIT_TIMEOUT if until else EXIT_OK
            wait = POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining)
            try:
                batch = session.collect_events(after=cursor, wait_seconds=wait)
            except (ActionError, IpcError, ThreadError) as exc:
                emit({"type": "watch_error", "error": type(exc).__name__, "message": str(exc)})
                return EXIT_ERROR
            if batch["dropped"]:
                emit({"type": "watch_dropped", "dropped": batch["dropped"]})
            for event in batch["events"]:
                emit(event)
            cursor = batch["cursor"]
            if any(e["type"] in until for e in batch["events"]):
                return EXIT_OK
    except KeyboardInterrupt:
        emit({"type": "watch_stopped", "signal": "SIGINT"})
        return EXIT_INTERRUPTED
    except Terminated as exc:
        emit({"type": "watch_stopped", "signal": signal.Signals(exc.signum).name})
        return 128 + exc.signum
    finally:
        for thread_id, instance in followed:
            # Best effort: the process is exiting and the app drops the
            # subscription along with the connection anyway.
            with contextlib.suppress(ActionError, IpcError, ThreadError):
                session.follow_thread(thread_id, follow=False, instance=instance)
        session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-pilot",
        description="Drive Codex Desktop threads from a shell.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    watch_cmd = sub.add_parser(
        "watch",
        help="stream thread events as JSON lines",
        description=(
            "Follow one or more threads and print each event as a JSON line. "
            "Made to be backgrounded by a shell rather than blocking a caller. "
            "A thread only streams while Codex Desktop has it mounted: an "
            "unmounted thread is silent, so a watch on one runs to its timeout. "
            "Bring it forward with the focus_thread MCP tool and retry."
        ),
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    watch_cmd.add_argument("thread", nargs="+", help="thread id, exact name, or unique substring")
    watch_cmd.add_argument(
        "--until",
        action="append",
        metavar="EVENT",
        choices=MATCHABLE_EVENTS,
        help="exit as soon as this event arrives (repeatable); one of: "
        + ", ".join(MATCHABLE_EVENTS),
    )
    watch_cmd.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="give up after this many seconds; 0 (the default) watches indefinitely",
    )
    # Two ways to say "this instance", and they cannot be combined: pinning a
    # CODEX_HOME builds a single instance outright, so there is no discovered
    # slug left for --instance to select.
    target = watch_cmd.add_mutually_exclusive_group()
    target.add_argument("--instance", default=None, help="restrict to one discovered instance slug")
    target.add_argument(
        "--codex-home",
        default=None,
        help="pin one instance by CODEX_HOME instead of discovering any; excludes --instance",
    )
    watch_cmd.set_defaults(func=watch)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse reports usage errors on stderr, which a stdout-reading
        # watcher never sees. --help and friends exit 0 having already printed.
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        if code != EXIT_OK:
            emit(
                {
                    "type": "watch_error",
                    "error": "UsageError",
                    "message": "invalid arguments; run 'codex-pilot watch --help'",
                }
            )
        return code
    handler: Any = args.func
    try:
        result: int = handler(args)
    except Exception as exc:
        # Anything unforeseen still owes the caller a line: exiting mute is the
        # one failure this command must never have.
        emit({"type": "watch_error", "error": type(exc).__name__, "message": str(exc)})
        return EXIT_ERROR
    return result


if __name__ == "__main__":
    sys.exit(main())
