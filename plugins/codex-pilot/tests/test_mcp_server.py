"""The MCP surface, exercised through a real client over stdio.

This is the contract Claude Code actually sees: tool names, schemas, and the
shape of what comes back. The Codex instances are stubbed out, so these run
without a Codex Desktop install.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from codex_pilot import mcp_server

REPO = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "list_threads",
    "thread_status",
    "send_message",
    "steer_turn",
    "stop_turn",
    "respond",
    "edit_thread",
    "set_goal",
    "focus_thread",
    "follow_thread",
    "collect_events",
}


def server_params(codex_home: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_pilot.mcp_server"],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(codex_home.parent),
            "CODEX_HOME": str(codex_home),
            "PYTHONPATH": str(REPO / "src"),
        },
    )


@pytest.fixture
def empty_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".codex"
    (home / "thread-writer-locks").mkdir(parents=True)
    return home


@pytest.mark.anyio
async def test_lists_every_tool(empty_home: Path):
    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        await sess.initialize()
        tools = await sess.list_tools()
        assert {t.name for t in tools.tools} == EXPECTED_TOOLS


@pytest.mark.anyio
async def test_every_tool_documents_itself(empty_home: Path):
    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        await sess.initialize()
        tools = await sess.list_tools()
        for tool in tools.tools:
            # These descriptions are the only guidance Claude gets before
            # calling something that drives a live agent.
            assert tool.description and len(tool.description) > 80, tool.name


@pytest.mark.anyio
async def test_server_instructions_carry_the_two_load_bearing_rules(empty_home: Path):
    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        result = await sess.initialize()
        instructions = result.instructions or ""
        assert "writer lock" in instructions
        assert "auto_review" in instructions


@pytest.mark.anyio
async def test_list_threads_on_an_empty_instance(empty_home: Path):
    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        await sess.initialize()
        result = await sess.call_tool("list_threads", {})
        payload = json.loads(result.content[0].text)
        assert payload["ok"] is True
        assert payload["threads"] == []


@pytest.mark.anyio
async def test_errors_come_back_as_data_not_exceptions(empty_home: Path):
    # Claude has to be able to read and act on a failure, so tools return the
    # error rather than raising through the protocol.
    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        await sess.initialize()
        result = await sess.call_tool("thread_status", {"thread": "no-such-thread"})
        payload = json.loads(result.content[0].text)
        assert payload["ok"] is False
        assert payload["error"] == "UnknownThreadError"


@pytest.mark.anyio
async def test_edit_thread_rejects_an_unknown_action(empty_home: Path):
    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        await sess.initialize()
        result = await sess.call_tool("edit_thread", {"thread": "x", "action": "explode"})
        payload = json.loads(result.content[0].text)
        assert payload["ok"] is False
        assert "unknown action" in payload["message"]


# -- the wait cap -------------------------------------------------------------
#
# Waiting inside the tool holds the caller's whole agent turn, so the wait is
# capped. The cap has to announce itself: a silently shortened wait looks
# exactly like one that ran its course and found nothing.


class StubSession:
    """Records the wait it was handed, and returns without doing any of it."""

    def __init__(self) -> None:
        self.wait_seconds: float | None = None

    def collect_events(self, threads, after=0, wait_seconds=0.0, instance=None):
        self.wait_seconds = wait_seconds
        return {"events": [], "cursor": after, "dropped": 0, "following": []}


@pytest.fixture
def stub_session(monkeypatch) -> StubSession:
    stub = StubSession()
    monkeypatch.setattr(mcp_server, "_session", stub)
    return stub


def test_a_wait_within_the_cap_is_passed_through_untouched(stub_session: StubSession):
    result = mcp_server.collect_events(wait_seconds=5.0)
    assert stub_session.wait_seconds == 5.0
    assert "note" not in result


def test_a_wait_over_the_cap_is_shortened_and_says_so(stub_session: StubSession):
    result = mcp_server.collect_events(wait_seconds=420.0)
    assert stub_session.wait_seconds == mcp_server.MAX_WAIT_SECONDS
    assert "note" in result, "a shortened wait must not be silent"
    assert "420" in result["note"]
    assert "codex-pilot watch" in result["note"]


def test_the_note_names_the_thread_the_caller_asked_about(stub_session: StubSession):
    result = mcp_server.collect_events(threads=["abc-123"], wait_seconds=420.0)
    assert "abc-123" in result["note"]
    assert "<thread>" not in result["note"]


def test_the_note_keeps_the_placeholder_when_several_threads_are_watched(
    stub_session: StubSession,
):
    result = mcp_server.collect_events(threads=["abc-123", "def-456"], wait_seconds=420.0)
    assert "<thread>" in result["note"]


def test_running_from_the_project_suggests_a_command_that_finds_it(tmp_path: Path):
    """This process runs inside the plugin's uv environment, where the console
    script is always on PATH -- the caller's shell is not, so the command has to
    carry --project or it fails with 'command not found'."""
    (tmp_path / "pyproject.toml").touch()
    assert mcp_server._watch_prefix_for(tmp_path) == f"uv run --project {tmp_path} codex-pilot"


def test_an_installed_wheel_suggests_the_bare_command(tmp_path: Path):
    # No pyproject beside us means an installed tool, whose script is on PATH.
    assert mcp_server._watch_prefix_for(tmp_path) == "codex-pilot"


def test_the_suggested_command_points_at_this_checkout(monkeypatch):
    command = mcp_server.watch_command("abc-123", 900)
    assert command.startswith("uv run --project /"), command
    assert command.endswith("codex-pilot watch abc-123 --until turn_completed --timeout 900")
    project = Path(command.split()[3])
    assert (project / "pyproject.toml").is_file(), f"{project} is not the project root"


def test_a_negative_wait_is_floored_without_claiming_it_was_capped(stub_session: StubSession):
    result = mcp_server.collect_events(wait_seconds=-5.0)
    assert stub_session.wait_seconds == 0.0
    assert "note" not in result


@pytest.mark.anyio
async def test_the_served_version_matches_what_ships(empty_home: Path):
    """Four places state the version; the handshake is the one a client sees, so
    a drifting literal there means nobody can tell which build they have."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    expected = pyproject["project"]["version"]
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    marketplace = json.loads(
        (REPO.parent.parent / ".claude-plugin" / "marketplace.json").read_text()
    )
    assert plugin["version"] == expected
    assert marketplace["version"] == expected

    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        result = await sess.initialize()
        assert result.server_info.version == expected


@pytest.mark.anyio
async def test_collect_events_sends_long_waits_to_the_shell_watch(empty_home: Path):
    # The description is the only thing read before the call, so the cheap
    # path (drain a cursor) and the unbounded one (background a watch) both
    # have to be in it.
    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        await sess.initialize()
        tools = {t.name: t for t in (await sess.list_tools()).tools}
        description = tools["collect_events"].description or ""
        assert "wait_seconds=0" in description
        assert "codex-pilot watch" in description
        # The cap is stated as a number in prose; keep it tied to the constant
        # so changing one cannot silently make the other lie.
        assert f"{mcp_server.MAX_WAIT_SECONDS:g}s" in description
        assert "note" in description, "the conditional field has to be documented"


@pytest.mark.anyio
async def test_respond_accepts_an_integer_request_id(empty_home: Path):
    # Request ids arrive from the app as integers; a schema that only took
    # strings would make every real approval unanswerable.
    async with (
        stdio_client(server_params(empty_home)) as (read, write),
        ClientSession(read, write) as sess,
    ):
        await sess.initialize()
        tools = {t.name: t for t in (await sess.list_tools()).tools}
        schema = tools["respond"].input_schema["properties"]["request_id"]
        rendered = json.dumps(schema)
        assert "integer" in rendered
