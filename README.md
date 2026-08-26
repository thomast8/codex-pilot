# codex-pilot

Drive Codex Desktop threads from Claude Code: send work, steer a running turn,
stop one, answer approval requests, change model / reasoning / plan mode, and
get told when a thread goes idle.

Built by decoding Codex Desktop's private local IPC protocol from the installed
app bundle and validating every call against the running app. The full decode is
in [`docs/protocol.md`](docs/protocol.md).

> Written with Claude (Opus 5), driven and reviewed by Thomas Tiotto. Every
> protocol claim marked *verified* was executed against a live Codex Desktop;
> anything decoded-but-unexecuted is labelled as such.

## What it does

| Tool | |
| --- | --- |
| `list_threads` | every thread across every installed Codex instance, with its route |
| `thread_status` | busy/idle, current turn, settings, and anything the thread is waiting on |
| `send_message` | start a turn — routes itself over IPC or a detached resume |
| `steer_turn` | inject into a *running* turn without restarting it |
| `stop_turn` | interrupt, optionally only if a specific turn is still running |
| `respond` | answer an approval, permission request, tool question or MCP elicitation |
| `edit_thread` | model, reasoning effort, plan mode, fast mode, sandbox, approvals; or compact |
| `set_goal` | give a thread a standing objective |
| `follow_thread` / `collect_events` | learn when a turn finished, without polling |
| `focus_thread` | make the app mount a thread it is holding but not showing |

## Install

Requires macOS, [uv](https://docs.astral.sh/uv/), and Codex Desktop.

```sh
claude mcp add --scope user codex-pilot -- /path/to/codex-pilot/bin/launch-mcp.sh
```

Or as a plugin, which ships the MCP server and the usage skill together:

```sh
/plugin marketplace add <your-marketplace>
/plugin install codex-pilot@<your-marketplace>
```

## The three things that decide what is possible

**The writer lock picks the route.** Codex allows one writer per thread. A thread
the app has open can only be driven over IPC; one nothing holds can only be
resumed detached. `send_message` chooses. The lock is never worked around — two
writers on one rollout corrupt it.

**A locked thread is not always a reachable one.** The app holds a writer lock on
every thread it has open but only answers for the one a window is *rendering*.
Probing 12 lock-holding threads, 5 answered and 7 did not. Such a thread can be
driven by neither route until something surfaces it — that is what `focus_thread`
is for. Remodex documents the same boundary: *"Codex Desktop only accepts an
external stream after that thread's route is mounted."*

**Approvals only appear when the thread asks a human.** With `approvalsReviewer`
set to `auto_review` (the default) a subagent silently decides escalations and
`thread_status` shows nothing pending — indistinguishable from a thread that
never asked. Set it to `user` on threads you intend to supervise.

## Deliberately not implemented

**Queued follow-ups.** The app's newer build has a full queue API
(`thread/queue/add|delete|list|reorder|start|update`), but the only method
exposed to followers is `thread-follower-set-queued-follow-ups-state`, and it
*replaces the whole queue* rather than appending. The queue contents are not in
`conversationState` and are not broadcast unsolicited, so there is no way to read
what is queued before replacing it. Implementing this would mean blind-writing
over follow-ups queued in the app. Not worth the data loss.

**`thread/revert`.** Exists in the app-server protocol; has no `thread-follower-*`
wrapper, so the follower surface cannot reach it.

**Owning threads outright.** Remodex takes the other road — it spawns its own
`codex app-server`, owns threads it starts, and lets Desktop follow *it*. That
makes threads reachable without the app rendering them, at the cost of running a
second runtime and holding the locks. codex-pilot is a pure follower by choice:
lighter, no contention, but bounded by what the app will surface.

## Development

```sh
uv run pytest                                    # 162 tests
uv run ruff check src tests scripts
uv run mypy
uv run python scripts/extract_registry.py --check  # protocol drift
```

`scripts/live_smoke.py` drives a real thread one method at a time. It requires an
explicit `--thread` and hard-codes no ids; point it at something disposable.

### When Codex Desktop updates

Every request carries a pinned protocol version, and a bumped one fails as
`no-client-found` on a thread the app visibly owns — the same error as "nobody
owns this thread". `scripts/extract_registry.py --check` diffs every installed
bundle against the pinned map, and a test does the same. Doppel clones carry
patched bundles, so each is checked separately.

## License

MIT
