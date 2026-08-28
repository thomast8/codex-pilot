from pathlib import Path

import pytest

from codex_pilot.threads import ServingApp


@pytest.fixture
def anyio_backend():
    """The MCP client is async; asyncio is the only backend we need."""
    return "asyncio"


STUB_APP = Path("/Applications/Stub Codex.app")


@pytest.fixture(autouse=True)
def link_target_is_not_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `link_target` off the machine the suite happens to run on.

    Aiming a deep link means asking which app serves an instance, and answering
    that for real means lsof, ps and whichever ChatGPT bundles are installed
    here. A test that fell through to those would pass or fail on the host's
    app inventory, which is the machine dependence `test_registry_drift` is
    meant to be the only instance of. Tests about the aiming itself override
    this with their own answer.
    """
    monkeypatch.setattr(
        "codex_pilot.actions.serving_app",
        lambda paths, *a, **kw: ServingApp(bundle=STUB_APP),
    )
