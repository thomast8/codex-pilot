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

## Waiting without burning a turn

`collect_events(wait_seconds=...)` can only wait by blocking the MCP call, and a
blocked tool call freezes the agent turn that made it — no checking another
thread, no reacting, no clean interrupt. Events accumulate in the background
either way, so the cheap move is to drain with `wait_seconds=0` and hand the
waiting to a shell command the harness can background. The tool's own wait is
capped at 30s for the same reason, and says so in the result when it shortens
what you asked for.

```sh
# one notification when the thread goes idle
codex-pilot watch <thread> --until turn_completed --timeout 900

# a line per event, for a streaming watch
codex-pilot watch <thread-a> <thread-b>
```

The CLI ships inside the plugin's uv project. Run it as
`uv run --project /path/to/plugins/codex-pilot codex-pilot watch ...` from
anywhere, or `uv tool install /path/to/plugins/codex-pilot` once to put
`codex-pilot` on your PATH.

Each event is one JSON object on stdout, flushed immediately, which is what a
line-oriented watcher can filter on. Trouble is reported the same way rather
than by going quiet: `watch_dropped` when the buffer overflowed, and `resync` or
`follow_lost` when the stream broke under it. Those three do **not** end the
watch — only `watch_timeout`, `watch_error`, a `--until` match, or a signal do,
and each prints its own line first. Silence therefore always means the watch is
still running.

`--until` waits for one of the follow subsystem's events: `turn_started`,
`turn_completed`, `request_pending`, `request_resolved`, `resync`,
`follow_lost`. The `watch_*` lines are the CLI talking about itself and are
rejected as `--until` values rather than never matching.

| Exit | Meaning |
|---|---|
| `0` | the `--until` event arrived, or a watch with no `--until` hit its timeout |
| `1` | error — thread could not be resolved, app unreachable |
| `2` | bad arguments |
| `3` | timed out before the requested `--until` event arrived |
| `130` / `143` | interrupted (SIGINT) / terminated (SIGTERM) |

With no `--timeout`, a watch runs indefinitely — including after a
`follow_lost` it could not recover from. Pass an explicit `--timeout` whenever
you need the run to be bounded.

The mount constraint applies here as everywhere: a thread the app holds but is
not rendering streams nothing, so a watch on one runs to its timeout. Surface it
with `focus_thread` first.

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
uv run pytest                                    # 216 tests
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
