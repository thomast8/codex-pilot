import pytest


@pytest.fixture
def anyio_backend():
    """The MCP client is async; asyncio is the only backend we need."""
    return "asyncio"
