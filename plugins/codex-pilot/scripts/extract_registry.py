#!/usr/bin/env python3
"""Extract the IPC method version map from a Codex Desktop app bundle.

The registry in `codex_pilot/registry.py` is a copy of the `b_` object in the
app's Electron main bundle. When Codex Desktop updates, a bumped version turns
into `no-client-found` on a thread the app visibly owns -- the same error as
"nobody owns this thread" -- so this script exists to make the diff a one-liner
rather than an afternoon.

    uv run python scripts/extract_registry.py            # every installed app
    uv run python scripts/extract_registry.py --check    # exit 1 on any drift

Doppel clones carry their own patched `app.asar`, so each installed bundle is
checked separately: one clone can drift while the stock app does not.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_pilot.instances import APP_GLOB, DEFAULT_SEARCH_DIRS  # noqa: E402
from codex_pilot.registry import METHOD_VERSIONS  # noqa: E402

# The map is an object literal whose first key is the stream-state method.
REGISTRY_RE = re.compile(r'\{"thread-stream-state-changed":\d+[^}]*\}')


def read_asar(path: Path) -> list[tuple[str, bytes]]:
    """Yield (inner path, bytes) for each file in an asar archive."""
    with path.open("rb") as fh:
        _, _, _, header_size = struct.unpack("<IIII", fh.read(16))
        header = json.loads(fh.read(header_size).decode("utf-8"))
        base = (16 + header_size + 3) // 4 * 4

        entries: list[tuple[str, int, int]] = []

        def walk(node: dict, prefix: str = "") -> None:
            for name, value in (node.get("files") or {}).items():
                inner = f"{prefix}/{name}"
                if "files" in value:
                    walk(value, inner)
                elif value.get("size"):
                    entries.append((inner, int(value.get("offset", 0)), int(value["size"])))

        walk(header)
        out: list[tuple[str, bytes]] = []
        for inner, offset, size in entries:
            if not inner.endswith(".js") or size > 8_000_000:
                continue
            fh.seek(base + offset)
            out.append((inner, fh.read(size)))
        return out


def registry_from_bundle(app: Path) -> dict[str, int] | None:
    asar = app / "Contents" / "Resources" / "app.asar"
    if not asar.exists():
        return None
    for _inner, blob in read_asar(asar):
        if b'"thread-stream-state-changed"' not in blob:
            continue
        match = REGISTRY_RE.search(blob.decode("utf-8", errors="replace"))
        if match:
            parsed = json.loads(match.group(0))
            return {str(k): int(v) for k, v in parsed.items()}
    return None


def installed_apps() -> list[Path]:
    apps: list[Path] = []
    for directory in DEFAULT_SEARCH_DIRS:
        try:
            apps.extend(sorted(directory.glob(APP_GLOB)))
        except OSError:
            continue
    return apps


def report(app: Path, found: dict[str, int]) -> bool:
    """Print any difference from the pinned registry. True when it matches."""
    added = {k: v for k, v in found.items() if k not in METHOD_VERSIONS}
    removed = {k: v for k, v in METHOD_VERSIONS.items() if k not in found}
    changed = {
        k: (METHOD_VERSIONS[k], v)
        for k, v in found.items()
        if k in METHOD_VERSIONS and METHOD_VERSIONS[k] != v
    }
    if not (added or removed or changed):
        print(f"  {app.name}: matches ({len(found)} methods)")
        return True
    print(f"  {app.name}: DRIFT")
    for k, (was, now) in sorted(changed.items()):
        print(f"    changed  {k}: {was} -> {now}")
    for k, v in sorted(added.items()):
        print(f"    added    {k}: {v}")
    for k, v in sorted(removed.items()):
        print(f"    removed  {k} (was {v})")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit non-zero on drift")
    ap.add_argument("--dump", action="store_true", help="print the map as Python")
    args = ap.parse_args()

    apps = installed_apps()
    if not apps:
        print("no ChatGPT*.app bundles found", file=sys.stderr)
        return 1

    ok = True
    for app in apps:
        found = registry_from_bundle(app)
        if found is None:
            print(f"  {app.name}: no registry found in bundle")
            ok = False
            continue
        if args.dump:
            print(f"# {app.name}")
            print(json.dumps(found, indent=4))
        elif not report(app, found):
            ok = False

    if args.check and not ok:
        print(
            "\nregistry drift -- update METHOD_VERSIONS in codex_pilot/registry.py", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
