"""Discovery of installed Codex Desktop instances.

One instance == one CODEX_HOME. That is the whole model, and it is what makes
running several ChatGPT apps side by side work without any special handling:
the IPC socket, session index, writer locks, rollouts and archive all live under
CODEX_HOME, so picking an instance is picking a directory.

Doppel (the user's multi-instance launcher) clones ChatGPT.app and stamps
`LSEnvironment.CODEX_HOME` into the clone's Info.plist, which macOS then puts in
the app's environment at launch. Reading those plists is therefore the
authoritative way to enumerate instances -- more reliable than guessing
directory names or looking at what happens to be running.

Consequence worth remembering: thread ids are unique *within* an instance, not
across them. A bare thread id is meaningless without knowing which CODEX_HOME it
came from.
"""

from __future__ import annotations

import os
import plistlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

APP_GLOB = "ChatGPT*.app"
PRODUCT_PREFIX = "ChatGPT"

DEFAULT_SEARCH_DIRS = (Path("/Applications"), Path.home() / "Applications")


def slug_for(bundle_name: str, is_default: bool) -> str:
    """Short handle for an instance, e.g. 'ChatGPT Personal' -> 'personal'."""
    if is_default:
        return "default"
    name = bundle_name.removesuffix(".app").strip()
    if name.startswith(PRODUCT_PREFIX):
        name = name[len(PRODUCT_PREFIX) :].strip() or name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "instance"


@dataclass(frozen=True)
class Instance:
    slug: str
    codex_home: Path
    app_path: Path | None
    is_default: bool

    def socket_candidates(self) -> list[Path]:
        """Where this instance's IPC socket may live, best first.

        The primary is the bundle's own resolver (`f9`): `$CODEX_HOME/ipc/ipc.sock`.
        The bundle also has a tmpdir fallback (`ece`), but that path is keyed only
        by uid, so it cannot tell two instances apart -- offer it to the default
        instance alone rather than risk driving the wrong app.
        """
        candidates = [self.codex_home / "ipc" / "ipc.sock"]
        if self.is_default:
            uid = os.getuid() if hasattr(os, "getuid") else None
            name = f"ipc-{uid}.sock" if uid is not None else "ipc.sock"
            candidates.append(Path(tempfile.gettempdir()) / "codex-ipc" / name)
        return candidates

    def socket_path(self) -> Path | None:
        """The first candidate that exists, or None if the app is not running."""
        for candidate in self.socket_candidates():
            try:
                if candidate.is_socket():
                    return candidate
            except OSError:
                continue
        return None

    @property
    def is_live(self) -> bool:
        return self.socket_path() is not None


def _codex_home_from_plist(app: Path) -> Path | None:
    info = app / "Contents" / "Info.plist"
    try:
        with info.open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    env = data.get("LSEnvironment")
    if not isinstance(env, dict):
        return None
    home = env.get("CODEX_HOME")
    return Path(home).expanduser() if isinstance(home, str) and home else None


def default_codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def installed_apps(search_dirs: list[Path] | None = None) -> list[Path]:
    """Every Codex Desktop bundle on this machine, stamped or not.

    Wider than `discover_instances`, and deliberately so: that keys by
    CODEX_HOME and drops the stock unstamped bundle, which is exactly the app a
    user is most likely to be looking at. Callers that need to recognise "a
    Codex window just came forward" want all of them.
    """
    out: list[Path] = []
    for directory in DEFAULT_SEARCH_DIRS if search_dirs is None else search_dirs:
        try:
            out.extend(sorted(directory.glob(APP_GLOB)))
        except OSError:
            continue
    return out


def discover_instances(
    search_dirs: list[Path] | None = None, default_home: Path | None = None
) -> list[Instance]:
    """Every Codex Desktop instance installed on this machine.

    The default instance is always included even when its bundle is missing or
    unstamped, because `~/.codex` is where a stock install keeps its state.
    Instances are keyed by CODEX_HOME, so two bundles pointing at one home
    collapse into a single instance rather than becoming rival targets.
    """
    dirs = list(DEFAULT_SEARCH_DIRS) if search_dirs is None else search_dirs
    home = default_home or default_codex_home()

    by_home: dict[Path, Instance] = {
        home: Instance(slug="default", codex_home=home, app_path=None, is_default=True)
    }

    for directory in dirs:
        try:
            apps = sorted(directory.glob(APP_GLOB))
        except OSError:
            continue
        for app in apps:
            app_home = _codex_home_from_plist(app)
            if app_home is None:
                # An unstamped ChatGPT.app is the stock install: it uses the
                # default home, which is already registered.
                continue
            if app_home in by_home:
                existing = by_home[app_home]
                if existing.app_path is None:
                    by_home[app_home] = Instance(
                        slug=existing.slug,
                        codex_home=existing.codex_home,
                        app_path=app,
                        is_default=existing.is_default,
                    )
                continue
            by_home[app_home] = Instance(
                slug=slug_for(app.stem, is_default=False),
                codex_home=app_home,
                app_path=app,
                is_default=False,
            )

    instances = list(by_home.values())
    instances.sort(key=lambda i: (not i.is_default, i.slug))
    return instances


def find_instance(slug: str, instances: list[Instance] | None = None) -> Instance | None:
    for inst in instances if instances is not None else discover_instances():
        if inst.slug == slug:
            return inst
    return None
