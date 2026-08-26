#!/bin/sh
# Bridge the plugin to the codex-pilot MCP server.
#
# uv resolves and caches the environment on first run, so the plugin needs no
# install step of its own. CLAUDE_PLUGIN_ROOT is set by Claude Code; the
# fallback keeps the script runnable directly for debugging.

ROOT="${CLAUDE_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"

if ! command -v uv >/dev/null 2>&1; then
	echo "codex-pilot: uv is required but not on PATH (https://docs.astral.sh/uv/)" >&2
	exit 1
fi

exec uv run --quiet --project "$ROOT" python -m codex_pilot.mcp_server "$@"
