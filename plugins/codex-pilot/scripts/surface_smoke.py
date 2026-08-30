#!/usr/bin/env python3
"""Prove a finished detached run really lands in a real Codex Desktop.

The suite can only show that the deep link was fired. Whether the app then
*renders* the thread is a fact about the app, and owner discovery answering for
it is the only thing that establishes it -- the same distinction `focus_thread`
draws between surfacing a thread and proving it mounted.

    uv run python scripts/surface_smoke.py --cwd /tmp/scratch-repo

`--cwd` is required and nothing is hard-coded, because this starts a real turn
against a real agent: point it at a directory you do not mind an agent seeing.
The turn itself is read-only and asks for one word, so the interesting part is
the few seconds after it exits.

Expect the Codex window to come forward once and the screen to be handed back.
That is the behaviour under test, not a side effect of it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_pilot.actions import Session  # noqa: E402

# Short, read-only, and uninteresting on purpose: this is about what happens
# when the run ends, so the run should end quickly and change nothing.
PROMPT = "Reply with exactly the word OK and nothing else. Do not read or modify any files."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", required=True, help="a disposable directory to work in")
    parser.add_argument("--instance", default=None, help="instance slug (default: the primary)")
    parser.add_argument("--model", default=None, help="model for the dispatch")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the run")
    args = parser.parse_args()

    session = Session()
    instance = session.instance_for(args.instance)
    print(f"instance={instance.slug} home={instance.codex_home} live={instance.is_live}")
    if not instance.is_live:
        print("no app is serving that instance -- start Codex Desktop first")
        return 2
    print(f"link target: {session.link_target(instance)}")

    try:
        started = session.start_thread(
            PROMPT, cwd=args.cwd, sandbox="read-only", model=args.model, effort="low"
        )
        thread = started["thread"]
        if thread is None:
            print(f"the run reported no thread id; see {started['log_path']}")
            return 1
        print(f"started {thread} (pid {started['pid']})")

        run = session.live_run(thread)
        if run is not None:
            run.wait(timeout=args.timeout)
        resolved = session.resolve(thread, instance.slug)

        # Before: the app has never had this thread, so nothing should answer.
        # A None here is the baseline the "after" is measured against, not proof
        # of anything on its own -- see `probe_mounted`.
        print(f"owner before: {session.probe_mounted(resolved, 1.0)}")

        session._reap_runs(instance)
        events = [e for e in session.collect_events()["events"] if e["thread"] == thread]
        if not events:
            print("no completion event -- that is a bug, not a slow app")
            return 1
        print(f"event: {events[-1]['type']}")
        print(f"surfaced: {json.dumps(events[-1]['data']['surfaced'], default=str)}")

        # After: the app answering owner discovery is what "it is on screen"
        # actually means, and it is the whole claim this script exists to check.
        for waited in range(1, 11):
            time.sleep(1)
            owner = session.probe_mounted(session.resolve(thread, instance.slug), 2.0)
            if owner is not None:
                print(f"owner after {waited}s: {owner}")
                print(f"route: {session.route_for(session.resolve(thread, instance.slug).info)}")
                return 0
        print("still unmounted after 10s -- the link fired but the app did not take it")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
