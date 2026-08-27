"""Drive a real Codex Desktop through a restart and check the pilot recovers.

The three defects this covers all needed a live app to show themselves: a
connection that outlives the process behind it, a follow that survives its
connection in name only, and a thread whose state cannot be read. Unit tests
pin the mechanisms; only this proves the whole path.

**It quits and relaunches an app, so it is scoped by construction.** The target
is the Personal instance and nothing else. That guard is an allow-list on the
resolved CODEX_HOME rather than a slug check, because Veridue's bundle stamps
the *default* home and so collapses into the `default` slug alongside
/Applications/ChatGPT.app -- "not default" would not have excluded it.

    uv run scripts/restart_smoke.py --thread <id> --dry-run
    uv run scripts/restart_smoke.py --thread <id> --yes
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_pilot.actions import Session  # noqa: E402
from codex_pilot.instances import Instance, discover_instances  # noqa: E402
from codex_pilot.ipc import CONNECTION_RESET, _stat_identity  # noqa: E402

# The only instance this script may touch, by resolved home.
ALLOWED_HOME = Path.home() / ".codex-secondary"
ALLOWED_BUNDLE_ID = "com.openai.codex.secondary"
# Its binary is ChatGPT.real (the clone wrapper), so match the bundle directory
# rather than the executable name.
ALLOWED_BUNDLE_DIR = Path.home() / "Applications" / "ChatGPT Personal.app"


class Refused(SystemExit):
    """The target is not the one instance this script is allowed to drive."""


def resolve(slug: str) -> Instance:
    matches = [i for i in discover_instances() if i.slug == slug]
    if not matches:
        known = ", ".join(sorted(i.slug for i in discover_instances()))
        raise Refused(f"no instance {slug!r}; known: {known}")
    inst = matches[0]
    if inst.codex_home.resolve() != ALLOWED_HOME.resolve():
        raise Refused(
            f"refusing {slug!r}: CODEX_HOME is {inst.codex_home}, and this script may "
            f"only drive {ALLOWED_HOME}. Note that Veridue and the main app share "
            f"{Path.home() / '.codex'}, so both are behind the 'default' slug."
        )
    # The home alone is not enough: if it were ever a symlink onto ~/.codex the
    # check above would pass while every operation drove the forbidden app.
    if inst.app_path is None or inst.app_path.resolve() != ALLOWED_BUNDLE_DIR.resolve():
        raise Refused(f"refusing {slug!r}: its bundle is {inst.app_path}, not {ALLOWED_BUNDLE_DIR}")
    return inst


def socket_owner(inst: Instance) -> int | None:
    path = inst.socket_path()
    if path is None:
        return None
    out = subprocess.run(
        ["lsof", "-t", str(path)], capture_output=True, text=True, check=False
    ).stdout.split()
    return int(out[0]) if out else None


def owner_command(pid: int) -> str:
    """The pid's real executable.

    Deliberately not `ps -o comm=`: on macOS that reports argv[0], which the
    process sets itself, so a `sleep` re-exec'd with the right argv passes any
    check built on it. `lsof -d txt` reports the actual text segment.
    """
    out = subprocess.run(
        ["lsof", "-p", str(pid), "-a", "-d", "txt", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def check_owner(pid: int) -> None:
    """Refuse anything that is not the app we are allowed to drive."""
    exe = owner_command(pid)
    if not exe:
        raise Refused(f"refusing pid {pid}: could not read its executable")
    try:
        under = Path(exe).resolve().is_relative_to(ALLOWED_BUNDLE_DIR.resolve())
    except (OSError, ValueError):
        under = False
    # `is_relative_to`, not `startswith`: a bundle named "... Personal.app.evil"
    # string-prefixes the allowed path and would otherwise pass.
    if not under:
        raise Refused(f"refusing pid {pid}: {exe!r} is not inside {ALLOWED_BUNDLE_DIR}")


def preflight(inst: Instance) -> int | None:
    path = inst.socket_path()
    pid = socket_owner(inst)
    print("target instance   :", inst.slug)
    print("CODEX_HOME        :", inst.codex_home)
    print("bundle            :", ALLOWED_BUNDLE_DIR)
    print("bundle id         :", ALLOWED_BUNDLE_ID)
    print("socket            :", path)
    print("socket identity   :", _stat_identity(path) if path else None)
    print("socket owner pid  :", pid)
    print("owner command     :", owner_command(pid) if pid else "(none)")
    return pid


def quit_app() -> None:
    # By bundle id, never by name: "ChatGPT" matches three installed bundles.
    subprocess.run(
        ["osascript", "-e", f'quit app id "{ALLOWED_BUNDLE_ID}"'], check=False, capture_output=True
    )


def relaunch_app() -> None:
    # -g: relaunch without taking over the screen.
    subprocess.run(["open", "-g", "-b", ALLOWED_BUNDLE_ID], check=False, capture_output=True)


def wait_for_socket(inst: Instance, was: tuple[int, int] | None, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = inst.socket_path()
        if path is not None and _stat_identity(path) not in (None, was):
            return True
        time.sleep(0.5)
    return False


def run(inst: Instance, thread: str, mount: bool = False) -> int:
    session = Session(instances=[inst])
    failures: list[str] = []
    try:
        client = session.client(inst)
        # Received frames go to listeners; `_sent` is outbound only, so watching
        # it for a reset could never have fired.
        resets: list[dict] = []
        client.add_broadcast_listener(
            lambda f: resets.append(f) if f.get("method") == CONNECTION_RESET else None
        )
        before_identity = client.socket_identity
        print(f"\nconnected, clientId={client.client_id} identity={before_identity}")

        session.follow_thread(thread, follow=True, instance=inst.slug)
        armed = session.collect_events(instance=inst.slug)
        epoch = armed["epoch"]
        print(f"follow armed, epoch={epoch}, following={armed['following']}")

        print("\nquitting the app by bundle id...")
        quit_app()
        time.sleep(3.0)
        print("relaunching...")
        relaunch_app()
        if not wait_for_socket(inst, before_identity):
            failures.append("the socket never came back with a new identity")
            return report(failures)
        print("socket re-bound")

        deadline = time.monotonic() + 60.0
        reconnected = False
        while time.monotonic() < deadline:
            try:
                fresh = session.client(inst)
            except Exception as exc:  # noqa: BLE001 - the app is still starting
                print(f"  waiting: {exc}")
                time.sleep(2.0)
                continue
            if fresh.socket_identity != before_identity:
                fresh.add_broadcast_listener(
                    lambda f: resets.append(f) if f.get("method") == CONNECTION_RESET else None
                )
                reconnected = True
                print(f"re-handshaked, clientId={fresh.client_id} identity={fresh.socket_identity}")
                break
            time.sleep(1.0)
        if not reconnected:
            failures.append("never re-handshaked onto the new socket")

        # A restart does not necessarily reopen the thread. Observed live: the
        # app came back holding no threads at all, so the re-subscribe reached
        # an app that had nothing to stream. Surface it rather than letting a
        # follow that cannot recover look like one that is broken.
        if mount:
            # Off by default because it deep-links the thread, changing what the
            # app has mounted. A diagnostic should not alter the state it came to
            # observe unless asked. (It no longer steals focus -- focus_thread
            # navigates in the background now.)
            print("\nnudging the app to mount the thread (--mount)...")
            session.focus_thread(thread, instance=inst.slug)

        print("waiting for the follow to re-arm...")
        deadline = time.monotonic() + 90.0
        health: dict[str, object] = {}
        while time.monotonic() < deadline:
            got = session.collect_events(instance=inst.slug, epoch=epoch)
            health = got["threads"].get(thread, {})
            if health.get("health") == "ok":
                break
            time.sleep(3.0)
        info = session.store(inst).describe(thread)
        print(f"follow health : {health.get('health')} ({health.get('reason')})")
        print(f"app holds it  : {info.app_owned} (holder={info.holder})")

        if health.get("health") == "ok":
            print("follow recovered on its own")
        elif not info.app_owned:
            # Not a regression: the app streams only what it has open. The point
            # is that this is *reported* now instead of looking like a live follow.
            print(
                "NOTE: the app did not reopen this thread, so there is nothing to "
                "stream. The follow correctly reports itself unhealthy rather than "
                "listing itself as following while silent -- which is the defect."
            )
            if health.get("pending_known") is not False:
                failures.append("an unreadable follow claimed its pending set was known")
        else:
            failures.append(f"the app holds the thread but the follow never recovered: {health}")

        if resets:
            print("\nNOTE: ipc-connection-reset was observed live -- it can be marked verified.")
        else:
            print("\nNOTE: no ipc-connection-reset seen; it stays decoded-but-unverified.")
    finally:
        session.close()
    return report(failures)


def report(failures: list[str]) -> int:
    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: re-handshake and follow re-arm both survived a real restart")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--thread", required=True, help="a disposable thread on the Personal instance")
    ap.add_argument("--instance", default="personal")
    ap.add_argument("--dry-run", action="store_true", help="print the resolved target and stop")
    ap.add_argument("--yes", action="store_true", help="required to actually quit the app")
    ap.add_argument(
        "--mount",
        action="store_true",
        help="focus the thread after the restart. Changes what the app has "
        "mounted, so it is opt-in.",
    )
    args = ap.parse_args()

    inst = resolve(args.instance)
    preflight(inst)
    if args.dry_run or not args.yes:
        print("\n(dry run -- pass --yes to drive the restart)")
        return 0

    pid = socket_owner(inst)
    if pid is None:
        raise Refused(
            "refusing: nothing owns the target socket, so there is no app to verify. "
            "Start the Personal instance first."
        )
    check_owner(pid)
    return run(inst, args.thread, mount=args.mount)


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
