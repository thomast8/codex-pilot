#!/usr/bin/env python3
"""Drive a real Codex Desktop thread, one protocol method at a time.

This is the only place the decoded-but-never-executed methods get fired for the
first time, so it is deliberately awkward to point at the wrong thread: there is
no default `--thread`, no thread id is hard-coded, and each phase is opt-in.

    uv run python scripts/live_smoke.py --thread ABC --phase read
    uv run python scripts/live_smoke.py --thread ABC --phase send
    uv run python scripts/live_smoke.py --thread ABC --phase steer
    uv run python scripts/live_smoke.py --thread ABC --phase stop

Run it against a disposable thread. `read` mutates nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_pilot.actions import Session  # noqa: E402
from codex_pilot.instances import discover_instances  # noqa: E402

# Must genuinely occupy the model for several seconds: a "count slowly" prompt
# completes in under 8s, which silently turns every interrupt test into a no-op
# against an already-idle thread.
SLOW_PROMPT = (
    "Write a detailed 4000-word essay on the history of hydraulic engineering. "
    "Take your time and be thorough. Do not use any tools."
)


def dump(label: str, value: object) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(value, indent=2, default=str)[:2000])


def phase_read(session: Session, ref: str, instance: str | None) -> None:
    print("instances:")
    for inst in discover_instances():
        print(f"  {inst.slug:10} home={inst.codex_home}  live={inst.is_live}")

    resolved = session.resolve(ref, instance)
    print(f"\nresolved {ref!r} -> {resolved.instance.slug}:{resolved.thread_id}")
    print(f"  name={resolved.name}  app_owned={resolved.info.app_owned}")
    holder = resolved.info.holder
    print(f"  holder={holder.described if holder else None}")
    print(f"  lock_known={resolved.info.lock_known}  archived={resolved.info.archived}")
    print(f"  cwd={resolved.info.cwd}")
    print(f"  rollout turn_id={resolved.info.turn_id}")

    owner = session.owner_of(resolved)
    print(f"\nowner clientId: {owner}")

    print("\ntaking a stream snapshot (transient follow)...")
    frame = session.snapshot(resolved)
    if frame is None:
        print("  NO SNAPSHOT within timeout")
        return
    params = frame.get("params") or {}
    change = params.get("change") or {}
    print(f"  change.type={change.get('type')}  revision={change.get('revision')}")
    out = Path("captured_snapshot.json")
    out.write_text(json.dumps(frame, indent=2))
    print(f"  saved raw frame -> {out} ({out.stat().st_size} bytes)")
    top = change.get("snapshot") or change.get("state") or {}
    if isinstance(top, dict):
        print(f"  snapshot top-level keys: {sorted(top)[:25]}")


def phase_send(session: Session, ref: str, instance: str | None) -> None:
    result = session.send_message(ref, "Reply with exactly OK and nothing else.")
    dump("send_message", result)
    print("\nCheck the app UI: the turn should appear and complete with 'OK'.")


def phase_steer(session: Session, ref: str, instance: str | None) -> None:
    resolved = session.resolve(ref, instance)
    print("starting a slow turn...")
    dump("send_message", session.send_message(ref, SLOW_PROMPT, instance))
    time.sleep(3)

    before = session.store(resolved.instance).describe(resolved.thread_id)
    print(f"rollout turn_id mid-turn: {before.turn_id}")

    dump(
        "steer_turn",
        session.steer_turn(
            ref, "Stop counting. Reply with exactly STEERED and nothing else.", instance
        ),
    )
    print("\nCheck the app UI: the steer should appear inside the running turn.")


def phase_stop(session: Session, ref: str, instance: str | None) -> None:
    resolved = session.resolve(ref, instance)
    print("starting a slow turn...")
    dump("send_message", session.send_message(ref, SLOW_PROMPT, instance))
    time.sleep(3)

    live = session.store(resolved.instance).describe(resolved.thread_id)
    print(f"rollout turn_id mid-turn: {live.turn_id}")

    # v3: no precondition.
    dump("stop_turn (v3, no expectedTurnId)", session.stop_turn(ref, instance=instance))

    if live.turn_id:
        print("\nrestarting for the v4 positive control...")
        session.send_message(ref, SLOW_PROMPT, instance)
        time.sleep(3)
        current = session.store(resolved.instance).describe(resolved.thread_id)
        dump(
            "stop_turn (v4, derived expectedTurnId)",
            session.stop_turn(ref, expected_turn_id=current.turn_id, instance=instance),
        )

    print("\nstale-id negative control (precondition should refuse, softly):")
    session.send_message(ref, SLOW_PROMPT, instance)
    time.sleep(3)
    result = session.stop_turn(
        ref, expected_turn_id="00000000-0000-0000-0000-000000000000", instance=instance
    )
    # The app answers ok:true with a null turn id and keeps running -- there is
    # no error to catch, so `stopped` is the assertion that matters.
    if result["stopped"]:
        print(f"  PRECONDITION NOT ENFORCED: {result}")
    else:
        print(
            f"  correctly refused (stopped={result['stopped']}, id={result['interrupted_turn_id']})"
        )
    session.stop_turn(ref, instance=instance)


PHASES = {"read": phase_read, "send": phase_send, "steer": phase_steer, "stop": phase_stop}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--thread", required=True, help="thread name or id (no default, on purpose)")
    ap.add_argument("--instance", default=None)
    ap.add_argument("--phase", required=True, choices=sorted(PHASES))
    args = ap.parse_args()

    session = Session()
    try:
        PHASES[args.phase](session, args.thread, args.instance)
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
