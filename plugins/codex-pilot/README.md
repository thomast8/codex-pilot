# codex-pilot

Drive Codex Desktop threads from Claude Code: start work in the repo or worktree
it belongs to, steer a running turn, stop one, answer approval requests, change
model / reasoning / plan mode, and get told when a thread goes idle.

Nothing blocks on Codex. Every call returns while the agent keeps working, so a
session that hands off a job stays free to do something else instead of sitting
on a synchronous `codex exec`. The one exception is `collect_events`, which waits
only if you ask it to and is capped at two minutes.

Built by decoding Codex Desktop's private local IPC protocol from the installed
app bundle and validating every call against the running app. The full decode is
in [`docs/protocol.md`](docs/protocol.md).

> Written with Claude (Opus 5), driven and reviewed by Thomas Tiotto. Every
> protocol claim marked *verified* was executed against a live Codex Desktop;
> anything decoded-but-unexecuted is labelled as such.

## What it does

| Tool | |
| --- | --- |
| `start_thread` | create a thread, optionally making it a worktree and branch, and start its first turn — at a model, reasoning effort and service tier you name per dispatch |
| `list_threads` | every thread across every installed Codex instance, with its route |
| `thread_status` | busy/idle, current turn, settings, and anything the thread is waiting on — plus what the rollout shows when the app will not stream |
| `send_message` | start a turn on an existing thread; routes itself over IPC or a detached resume, and a detached one takes per-turn model, effort and service tier |
| `steer_turn` | inject into a *running* turn without restarting it |
| `stop_turn` | interrupt a turn, or terminate a detached run and its process group |
| `respond` | answer an approval, permission request, tool question or MCP elicitation |
| `edit_thread` | model, reasoning effort, plan mode, fast mode, sandbox, approvals on a thread the app owns; or compact |
| `set_goal` | give a thread a standing objective |
| `follow_thread` / `collect_events` | learn when a turn finished, without polling — covers detached runs too, and reports each follow's health |
| `focus_thread` | make the app mount a thread it is holding but not showing |
| `sync_threads` | which threads the app will actually answer for, and mount the rest |
| `read_thread` | what a thread said, read off disk — works for every thread |

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

## The things that decide what is possible

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

**A lock holder is not always the app, and the name will not tell you.** A
detached `codex exec` holds the same lock, and `lsof` reports its command as
`codex` exactly like the app's own `codex app-server` child. What separates
them is the pid: the app's writer is the process listening on that instance's
IPC socket, or a descendant of it. That test is decided from the process table,
not from anything only the calling process remembers, so a second session — or
the same one after a restart — reaches the same verdict instead of mistaking
someone else's writer for the app. Deciding it from argv would be worse than
useless: a detached run's argv contains the whole prompt, and a prompt can say
anything.

**Not seeing something is not the same as there being nothing.** This is the one
rule that decides whether a supervising session notices a wedged agent or waits
on it forever. Stream state only reaches a thread the app has *mounted*, so
`thread_status` returns `state: null` for plenty of threads that are very much
doing something — and an empty pending list read exactly like "nothing to
answer".

So the answer is now in a field rather than in prose. `pending_known: false`
means the pending set could not be read at all. Alongside it, `disk` reports what
the rollout still shows: `phase` is `mid_turn` or `idle`, `last_boundary` is the
record it read that from, and `last_boundary_at` is when that boundary was
written. `age_seconds` comes from the thread store rather than the rollout — time
since the thread's last recorded activity, and null if there is none. A
`mid_turn` phase with a large `age_seconds` is the signature of an app that
stopped answering. The rollout cannot supply the pending request itself — Codex writes no
record for one, checked across 1,096 real rollouts (which mostly ran
`auto_review`, so see `docs/protocol.md` for the exact scope) — but "abandoned mid-turn" and
"idle" are different enough to act on.

`collect_events` carries the same idea for follows. Each thread reports `health`
(`ok`, `resyncing`, `lost`, `not_following`) with its own `pending_known`, so a
thread that is unwatched because the server restarted says so instead of simply
being absent from `following`. Pass the `epoch` back with your `cursor`: sequence
numbers restart with the process, and without it a stale cursor silently
discards every event that follows, forever.

**A blocked thread is usually a settings problem.** A thread has no network by
default, and a command needing one fails as a DNS error rather than as a policy
refusal. `edit_thread` grants it: `sandboxPolicy` carries `networkAccess` and
`writableRoots`. Verified live, a thread that could not resolve a host answered
HTTP 200 on the next turn after the grant. Note `permissions` is a named profile
id string, not a permission object, and the two cannot be combined.

**Approvals only appear when the thread asks a human.** With `approvalsReviewer`
set to `auto_review` (the default) a subagent silently decides escalations and
`thread_status` shows nothing pending — indistinguishable from a thread that
never asked. Set it to `user` on threads you intend to supervise.

**Creating a thread is the one thing the IPC surface cannot do.** The app's
follower protocol only drives threads that already exist, and there is no
`codex://new` deep link, so `start_thread` spawns `codex exec` detached and
reads the new thread id off its JSONL. It returns in about a second, works in
the directory you name rather than the caller's, and the thread it creates can
be pulled into Codex Desktop afterwards with `focus_thread`.

That introduces a third lock state, which the rest of the surface is built
around rather than papering over. While such a run is going, the lock holder is
a `codex exec`, not the app: `route` reads `detached_running`, every app-driven
verb refuses instead of walking you into a second writer on the rollout, and
`stop_turn` terminates the run's process group. When it exits, the run reports
`turn_completed` on the same event stream a followed thread uses, so one
`collect_events` waits on app threads and detached runs alike.

The run does not have to be one this process started. A writer belonging to
another session reads as `detached_running` just the same, with no
`started_here` on the row — and then `stop_turn` refuses along with everything
else, because that process group is not ours to signal. This is what Codex
Desktop's *"This is open in another app"* banner looks like from the other side:
the thread is being written to the whole time, the app simply cannot attach to
show it.

**Codex's own worktrees are not reachable from here.** Codex Desktop can run a
thread in its own git worktree and fork a conversation into one, but that is an
app-side affordance: no IPC method creates a thread, and a thread started from
here does not hold the app-control tools that do it — asked to fork itself into a
worktree it delegates to a subagent that hand-rolls `git worktree add` into /tmp
on a detached HEAD, which is a directory rather than a managed worktree.

So `start_thread` makes them itself, in the same place and layout Codex uses
(`<CODEX_HOME>/worktrees/<id>/<repo>`, overridable with
`CODEX_PILOT_WORKTREE_ROOT`), on a branch the caller names, refusing one that
already exists. Parallel work
either starts in the app (and codex-pilot drives the threads it produces) or uses
worktrees you made yourself, kept out of Codex's own worktree root, which it
garbage-collects.

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
uv run pytest                                    # 317 tests
uv run ruff check src tests scripts
uv run mypy
uv run python scripts/extract_registry.py --check  # protocol drift
```

`scripts/live_smoke.py` drives a real thread one method at a time. It requires an
explicit `--thread` and hard-codes no ids; point it at something disposable.

`scripts/restart_smoke.py` quits and relaunches a Codex Desktop to prove the
pilot re-handshakes and re-arms its follows. Because it kills an app, it refuses
any instance whose resolved `CODEX_HOME` is not the one it is scoped to, targets
bundles by id rather than by the name `ChatGPT` (which matches every clone), and
checks a pid against the bundle directory before signalling it. `--dry-run`
prints the resolved target and stops; nothing destructive happens without
`--yes`.

### When Codex Desktop updates

Every request carries a pinned protocol version, and a bumped one fails as
`no-client-found` on a thread the app visibly owns — the same error as "nobody
owns this thread". `scripts/extract_registry.py --check` diffs every installed
bundle against the pinned map, and a test does the same. Doppel clones carry
patched bundles, so each is checked separately.

## License

MIT
