"""The MCP surface, exercised through a real client over stdio.

This is the contract Claude Code actually sees: tool names, schemas, and the
shape of what comes back. The Codex instances are stubbed out, so these run
without a Codex Desktop install.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
