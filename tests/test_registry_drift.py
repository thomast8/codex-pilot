"""Guard the pinned version registry against the app actually installed here.

A Codex Desktop update that bumps a version turns every call to that method into
`no-client-found` on a thread the app visibly owns -- which reads as "nobody owns
this thread" rather than "your protocol is stale". This test makes that failure
loud and local instead.

Skipped where no app bundle is installed (CI, a fresh checkout).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from codex_pilot.registry import METHOD_VERSIONS  # noqa: E402

extract_registry = pytest.importorskip("extract_registry")


def installed() -> list[Path]:
    return extract_registry.installed_apps()


@pytest.mark.skipif(not installed(), reason="no Codex Desktop app installed")
@pytest.mark.parametrize("app", installed(), ids=lambda p: p.stem)
def test_installed_bundle_matches_the_pinned_registry(app: Path):
    found = extract_registry.registry_from_bundle(app)
    assert found is not None, f"no version map found in {app}"
    # Doppel clones carry a patched app.asar, so each bundle is checked on its
    # own: one clone can drift while the stock app does not.
    assert found == METHOD_VERSIONS
