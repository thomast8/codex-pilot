# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Where the code is

The repo root holds the Claude Code plugin marketplace manifest
(`.claude-plugin/marketplace.json`) and little else. The Python project lives in
`plugins/codex-pilot/`, and **every command below runs from there** — the root
has no `pyproject.toml`.

## Commands

```sh
uv run pytest                                      # the suite
uv run pytest tests/test_follow.py -k resync       # one test
uv run ruff check src tests scripts
uv run mypy                                        # strict, src/codex_pilot only
uv run python scripts/extract_registry.py --check  # protocol drift vs installed app
```

There is no CI. The suite is the gate, and it is machine-dependent in one place:
`tests/test_registry_drift.py` parametrises over the Codex Desktop bundles
actually installed here and skips entirely when there are none. Passing on a
machine without the app proves less than passing on one with it.

Three smoke scripts drive a **real** app and are not part of the suite:

```sh
uv run python scripts/live_smoke.py --thread <id>       # one method at a time
uv run python scripts/restart_smoke.py --thread <id> --dry-run
uv run python scripts/surface_smoke.py --cwd <dir>      # a finished run reaching the app
```

`live_smoke.py` requires an explicit `--thread` and hard-codes no ids — point it
at something disposable, it sends real turns to a real agent. `restart_smoke.py`
quits and relaunches Codex Desktop to prove reconnect recovery, so it is
allow-listed to one CODEX_HOME by construction; run `--dry-run` before `--yes`.
`surface_smoke.py` starts a throwaway thread in the `--cwd` you name and checks
that the app renders it once the run exits — the suite can only show the link
was fired, and whether the app took it is a fact about the app.

## Layout

Transport is `framing` (length-prefixed JSON) → `registry` (pinned method
versions, envelope building) → `ipc` (one long-lived socket client per instance,
with a read pump, connection retirement and re-handshake). Discovery is
`instances` (one Codex install == one CODEX_HOME) and `threads` (ids, cwd,
writer-lock holders via lsof). `payloads` and `snapshot` are pure — request
builders and a projection of the app's ~118KB `conversationState` down to the few
facts we act on — so they are testable without a socket. `follow` keeps a
subscription current and turns transitions into events; `transcript` reads the
same story off the rollout on disk; `resume` is the detached `codex exec resume`
route and `worktrees` makes the git worktree a new thread runs in.
`actions.Session` ties all of it together, and `mcp_server` and `cli` are two
frontends over the same Session.

## The invariants any change has to respect

**The writer lock picks the route, and there are three.** Codex allows one writer
per thread. `desktop` means the app has it open and only IPC reaches it;
`detached` means nothing holds it and only `codex exec resume` reaches it;
`detached_running` means one of *our own* children holds it, and neither route
works until that process exits. Never contend for the lock — two writers on one
rollout corrupt it. `send_message` chooses; nothing else should.

**Locked is not the same as mounted.** The app holds a writer lock on every thread
it has open but only answers owner discovery, and only streams, for one a window
is actually rendering — 4 of 13 lock-holders on a real instance. Such a thread is
reachable by neither route until `focus_thread` surfaces it. This is the common
case, not an edge case, and it is why `transcript` exists: the rollout is the tier
that always works.

**Instance binding is atomic.** Thread ids are unique within a CODEX_HOME, not
across them. Socket, lock dir, rollout and archive for one call must all come from
the same `Instance`. Mixing them drives the wrong app with no error.

**Never guess a protocol version.** Every request carries a version pinned from
the installed app bundle. A wrong one comes back as `no-client-found`, which is
indistinguishable from "nobody owns this thread" — hours of the wrong debugging.
`UnknownMethodError` refuses to guess on purpose; re-extract `b_` with
`scripts/extract_registry.py` instead of inventing an entry.

**Absence is never reported as fact.** This is the bug this codebase keeps
finding in itself, in five places so far, so treat it as the house rule. A gap in
the stream drops the state and re-seeds it rather than applying a patch to the
wrong baseline (`resync`). An unreadable pending set returns `pending_known:
false`, not an empty list. A wait that gets clamped says what was asked for and
what was granted. A pump that hits an exception it did not expect marks every
follow `lost` with the reason and backs off, rather than looping silently while
callers believe they are still subscribed. A connection that stops answering is
retired and re-handshaked rather than held forever — but nothing is re-sent, so a
request whose outcome was unknown stays unknown. If you cannot know something,
say so in the return value; do not encode it as a benign-looking default.

## Protocol claims

`docs/protocol.md` is the decode, and its sourcing rules are project rules:
the installed app bundle (`app.asar`) outranks the app-server JSON schema, which
outranks [remodex](https://github.com/Emanuele-web04/remodex) — corroboration
only, and it targets a different build whose payload shapes disagree. Note also
that the app bundles its own `codex` binary, ahead of the one on PATH; the bundled
one is the schema source.

Every claim in that file is labelled **verified** (executed against a live Codex
Desktop) or **decoded** (read out of the bundle, never run). Keep the labels
honest when adding claims — the README makes the same promise to readers.

## Test harness

`tests/fakeapp.py` is a real router on a real AF_UNIX socket, because a reconnect
test has to prove what the app was told after the socket was replaced. macOS caps
AF_UNIX paths at ~104 bytes and pytest's `tmp_path` already exceeds that, so new
socket tests must bind under a short `/tmp` directory rather than `tmp_path`.

## Behaviour docs

The tool surface, approval-decision semantics, settings keys, worktree conventions
and `codex-pilot watch` exit codes are owned by
`plugins/codex-pilot/skills/driving-codex-desktop/SKILL.md` and
`plugins/codex-pilot/README.md`. Change them there; do not restate them here.
