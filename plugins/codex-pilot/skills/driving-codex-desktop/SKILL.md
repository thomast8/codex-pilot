---
name: driving-codex-desktop
description: Drive Codex agents from Claude Code - start a new thread in a given repo or worktree, send work, steer a running turn, stop one, answer approval requests, change model/reasoning/plan mode, set goals, and fan work out across several threads. Use whenever handing work to Codex or supervising it, instead of running `codex exec` or the standalone codex MCP server, and when a codex-pilot tool returns something unexpected.
---

# Driving Codex Desktop threads

`codex-pilot` drives real agents doing real work. A steer lands mid-thought; a
stop discards what a turn had in flight; an approval runs a command. Read
`thread_status` before acting on a thread you did not just act on.

## Which verb

| Situation | Use |
| --- | --- |
| Thread needs network, or wider write access | `edit_thread` (`sandboxPolicy`) |
| New work, no thread for it yet | `start_thread` (never `codex exec`) |
| Thread exists and is idle, you have new work | `send_message` |
| Turn is running and heading the wrong way | `steer_turn` |
| Turn is running and should stop | `stop_turn` |
| Thread is waiting on an approval or a question | `respond` |
| Change model, reasoning, plan mode, fast mode, sandbox | `edit_thread` (`update_settings`) |
| Give it a standing objective across turns | `set_goal` |
| Context is getting long | `edit_thread` (`compact`) |
| Read what a thread said, any thread | `read_thread` |
| Find out which threads are reachable | `sync_threads` |

`send_message` on a busy thread queues behind the running turn rather than
interrupting it. To change what the *current* turn is doing, steer.

## Nothing here blocks on Codex

Every tool in this plugin returns straight away. `send_message` hands back the
turn id with status `inProgress` in well under a second and does not wait for
the answer; `thread_status` reads projected state in milliseconds;
`start_thread` returns as soon as the thread has an id.

The one deliberate exception is `collect_events(wait_seconds=N)`, which blocks
because being told is the point, and it is capped at 120 seconds.

So never reach for a blocking route to "just get the answer". Start the work,
do something else, and collect the result when the thread reports idle.

## Starting new threads

`start_thread(text, cwd=...)`. It creates the thread, starts its first turn, and
returns the new thread id in about a second while the agent keeps working.

**Do not use `codex exec` from Bash, and do not use the standalone `codex` MCP
server, to start Codex work.** Both block until the agent finishes, so the
session sits idle for however long the task takes, and both inherit whatever
directory they were called from. `start_thread` has neither problem, and the
thread it creates is drivable by every other tool here.

### `cwd` is not optional, and not your own directory

`cwd` is where the agent works. Pass the repo or worktree the work belongs to.
Your own working directory is almost never the right answer, and a thread
started in the wrong one edits the wrong checkout.

**If the work is a distinct slice, give it a worktree first.** Two agents in one
checkout overwrite each other; that is the whole reason to isolate them. Create
it with the tools you already have, then point the thread at it:

    git worktree add -b feature/<slice> <path>     # or the EnterWorktree tool
    start_thread(text="...", cwd="<path>")

codex-pilot does not create worktrees itself, on purpose: where they belong is a
per-repo convention, not something this plugin should decide. Codex Desktop does
create its own, but not for threads started from here — see *Parallel work* below
for which mechanism applies.

### After it finishes

While it runs, the CLI holds the thread's writer lock, so its `route` reads
`detached_running` and every app-driven verb refuses rather than fighting the
lock. `stop_turn` still works: on a run started here it terminates the process
group. Once it goes idle:

- `focus_thread` pulls it into Codex Desktop, after which IPC works on it
  normally: steer, stop, respond, follow. Only once it is idle — never while it
  is running.
- `log_path` holds streamed JSONL for the run; `thread_status` gives the
  `rollout` path for the full transcript.

## The writer lock decides the route

Codex allows one writer per thread, and `send_message` picks accordingly:

- **App has the thread open** → IPC. Everything works: steer, stop, respond.
- **Nothing holds it** → resumed detached. Returns a `pid` and `log_path`
  immediately and runs in the background. Steer and stop are *not* available;
  poll the log. An archived thread is unarchived first, reported as
  `unarchived: true`.

- **One of our own detached runs holds it** (`route: detached_running`) →
  neither driving route works until it exits. Wait for `turn_completed`, read
  its log, or `stop_turn` it. Do not focus it into the app meanwhile.

- **App holds it but is not *showing* it** → neither route works. The app locks
  every thread it has open but only answers for the one a window is rendering,
  so this is common. `UnclaimedThreadError` says so; call `focus_thread`, wait a
  couple of seconds, and retry. If focusing does not help, then suspect protocol
  drift after an app update and run `scripts/extract_registry.py --check`.

Never try to defeat the lock. Two writers on one rollout corrupt it.

## Approvals

**Nothing appears while `approvalsReviewer` is `auto_review`.** That is the
default. A subagent silently decides escalations and `thread_status` shows an
empty `pending` list — which looks identical to a thread that never asked for
anything. To supervise a thread, set it first:

    edit_thread(thread, "update_settings", {"approvalsReviewer": "user"})

When a request is pending, `thread_status` gives you `request_id`, `kind`,
`summary` (the exact command or file), `reason`, `cwd`, and
`available_decisions`. Read the command before answering — that is the whole
point of being in the loop.

**Pass `available_decisions` straight through to `respond`.** The valid answers
differ per request: a network-blocked command offers `accept`,
`acceptWithExecpolicyAmendment` and `cancel`, but *not* `decline`. An answer the
app did not offer is ignored rather than refused, so it would look like nothing
happened.

Know which decisions outlive the turn:

| Decision | Scope |
| --- | --- |
| `accept` / `decline` | this request only |
| `cancel` | denies **and interrupts the turn** |
| `acceptForSession` | stops prompting for matching commands for the rest of the session |
| `acceptWithExecpolicyAmendment` | writes a persistent execpolicy rule |
| `applyNetworkPolicyAmendment` | writes a persistent network rule for a host |

The bottom three are standing grants, not answers. Choose one deliberately or
not at all; `accept` is the per-request answer.

`respond` sends once and never retries. A timeout means the outcome is unknown —
check the app rather than resending, or you may answer a different request that
has taken the same slot.

## Reading state

`thread_status` returns `state: null` when it could not read. **That is not
"nothing is pending."** Branch on `pending_known`, not on an empty list: false
means the pending set could not be read at all.

When it is false, `disk` reports what the rollout still shows.
`disk.phase: "mid_turn"` is a thread left inside a turn — with a large
`age_seconds` that is a stalled agent, and the move is `focus_thread` (which
navigates in the background and does not steal the screen) or a look at the app.
`disk.phase: "idle"` means it finished its last turn, so a null state there is
much less interesting. `disk: null` means even that was unreadable.

The rollout never carries the pending request itself — Codex writes no record for
one — so a disk block is evidence about liveness, never about approvals.

Following a thread has the same rule. `collect_events` reports each thread's
`health`: `ok`, `resyncing` (asked, nothing back yet), `lost` (the connection
dropped), or `not_following` (never followed, or lost with the server process).
Only `ok` means an empty `pending` is real, and each entry carries its own
`pending_known` to say so. Pass `epoch` back with `cursor` — sequence numbers
restart with the server, and a stale cursor otherwise discards everything after
it in silence; a mismatch comes back with `cursor_reset: true`.

`stop_turn` reports `stopped`. Do not read `ok` — the app answers `ok: true`
even when it stopped nothing, both for an already-idle thread and for an
`expected_turn_id` that did not match the running turn. Pass
`expected_turn_id` from `thread_status` when you mean "stop *that* turn".

## Settings

`update_settings` applies to the **next** turn, not the running one.

- `model`, `effort` (reasoning), `personality`, `summary`
- `serviceTier` — `priority` is fast mode; also `default`, `flex`, `scale`
- `collaborationMode` — plan mode. Needs both halves:
  `{"mode": "plan", "settings": {"model": "<model>"}}`. `{"mode": "plan"}` alone
  is rejected.
- `approvalPolicy`, `approvalsReviewer`
- `sandboxPolicy` — an object, and where `networkAccess` lives:
  `{"type": "workspaceWrite", "networkAccess": true, "writableRoots": [...]}`.
  Types are camelCase (`workspaceWrite`, `readOnly`, `dangerFullAccess`).
- `permissions` — a named profile **id string**, not a permission object, and
  not combinable with `sandboxPolicy`. See *When a thread is blocked*.
- `multiAgentMode`, `cwd` — note that multi-agent means *subagents inside the
  thread*, sharing its working directory. It is not a way to get parallel work
  in separate worktrees; see *Parallel work* below.

## Several Codex apps

Each install has its own `CODEX_HOME`, and **thread ids are unique only within
one**. `list_threads` tags every thread with its instance. If a name exists in
two, the tool refuses rather than guessing — pass `instance` to choose.

## Waiting for a thread

`follow_thread` starts a background collector. Events accumulate whether or not
you are polling, so `collect_events(after=<cursor>, wait_seconds=0)` returns
everything you missed instantly — you lose nothing by not blocking.
`turn_completed` means the thread is free for more work; a non-zero `dropped`
means the buffer overflowed.

**Do not block on a long `wait_seconds`.** The call holds your entire turn while
it waits: you cannot check another thread, react to anything, or be interrupted.
It is capped at 30s for that reason, and the result says so when it shortens
your request. To wait out a turn that takes minutes, background a shell watch
and let the harness wake you:

```sh
codex-pilot watch <thread> --until turn_completed --timeout 900
```

**Do not guess that invocation.** The CLI ships inside the plugin's uv project,
so it is only on PATH if it was installed as a tool; otherwise it needs
`uv run --project <the plugin directory>` in front. You do not have to work out
which: call `collect_events` with a `wait_seconds` above the cap once and the
`note` in the result hands you the exact command, absolute path and all, for
this machine.

It prints one JSON event per line and exits when the thread goes idle, so
running it in the background costs no turn at all. `--until request_pending`
does the same for catching an approval the moment it appears. Exit codes: 0 got
the event, 3 timed out without it.

Between the two, prefer draining with `wait_seconds=0` whenever you are already
doing other work, and a background watch whenever you have nothing to do but
wait.

A follow only streams while the app has the thread **mounted**. If a follow stays
silent on an app-owned thread, `focus_thread` and retry.

Runs started by `start_thread` need no follow: they are watched as processes, and
their exit arrives on the same stream as `turn_completed` (or `run_failed`), so
one `collect_events` covers both kinds.

## Queued follow-ups are not available

The only follower method replaces the entire queue, and its contents cannot be
read. Using it would blind-overwrite follow-ups queued in the app. To hand work
to a busy thread, either `steer_turn` it now or wait for `turn_completed` and
`send_message` then.

## When a thread is blocked

Work stalls for two different reasons and they have opposite remedies. Say which
one you are in before reaching for either.

### Codex is blocked, and you can unblock it

Most of these are settings, not walls, and `edit_thread` changes them.

**No network.** A thread cannot reach the network by default: `workspaceWrite`
sets `networkAccess` to false, and a command that needs it fails as a DNS error
(`Could not resolve host`), which reads like a broken box rather than a policy.
Grant it:

    edit_thread(thread, "update_settings",
                {"sandboxPolicy": {"type": "workspaceWrite", "networkAccess": true}})

Applies to the next turn, so grant first, then send the work.

**`permissions` is not that switch.** It is a named profile id, a *string*.
Passing the plausible `{"network": {"enabled": true}}` fails with "invalid type:
map, expected a string", which tells you nothing. The two fields cannot be
combined. Sandbox types are camelCase here — `workspaceWrite`, `readOnly`,
`dangerFullAccess` — not the CLI's `workspace-write`.

**It needs to write outside its workspace.** Add `writableRoots` to the same
policy rather than escalating to `dangerFullAccess`.

**It is waiting on an approval.** Only if it can ask: a detached run has no way
to, which is why `start_thread` defaults to `approval="never"`. On an app-owned
thread set `approvalsReviewer` to `user`, then answer with `respond`.

### You are blocked, and Codex is the way through

The other direction, and the one that gets missed. When *your own* permissions
stop you — a command your harness will not run, an allowlist you cannot edit —
that is not a reason to give up on the task. Codex is a separate agent with its
own sandbox and its own approval system, and you can grant it what it needs. So
hand the work to a thread instead of abandoning it: set the sandbox policy it
requires, send the work, and answer its approvals with `respond`.

Do not invert this by reflex. Delegating is right when the block is your
harness's policy about *how you run commands*; it is wrong when the block exists
because the action itself is dangerous or the user has said no. Codex is not a
way around a decision the user made — if they declined something, it stays
declined, whoever would run it.

### Neither can proceed

Then say so plainly, name the exact blocker, and hand it back. A missing
credential, an account the user must log into, a decision that is theirs: none of
these are dissolved by trying the other agent. Report what is needed rather than
looping.

## Getting state for every thread

Three sources, and only the middle one can be silenced. Reach for the cheapest
that answers your question.

| Want | Use | Works for |
| --- | --- | --- |
| What a thread said, did, or decided | `read_thread` | **every** thread: mounted or not, running or idle, app-owned or detached |
| Live state now: busy, turn id, pending approvals | `follow_thread` + `collect_events` | only threads the app has **mounted** |
| Which threads are even reachable | `sync_threads` | app-owned threads |

**`read_thread` is the one that always works**, because it reads the rollout off
disk and never touches the app. Harvest with it. Re-running an agent to find out
what it already did is the mistake it exists to prevent.

**Mounting is a set, not a spotlight.** The app keeps a bounded set of threads
mounted and answers only for those; a thread it holds without rendering sends no
stream state at all, so a follow on one is silently empty. `sync_threads` tells
you which are mounted, and with `mount=true` brings the rest forward.

**Do not cycle through threads to "check" them.** Mounting is additive —
bringing one forward evicts none of the others — so `sync_threads` is a one-off
warm-up for the threads you intend to watch, and then their events simply
arrive. Rotating focus would also drag the app to the foreground each time, and
probing an unmounted thread costs the router's full discovery timeout.

## Parallel work: which worktree, and who makes it

Codex has worktree support of its own, and it is the better mechanism when you
can reach it — but you usually cannot reach it from here. Know which lane you
are in before fanning anything out.

**Codex Desktop's own worktrees.** A thread can run in its own git worktree on
its own branch, under the root set in Settings → Git (default
`~/.codex/worktrees/<id>/<repo>`). The composer toggles between working locally
and in a new worktree, `/fork` forks a conversation into one, and there is a
handoff flow that moves the changes back to the local checkout afterwards. Codex
creates the branch, tracks the worktree, and cleans it up later.

**codex-pilot cannot start that.** There is no IPC method that creates a thread,
and a thread `start_thread` created does not hold the app-control tools that fork
into a worktree — those belong to threads Codex Desktop made itself. Asking one in
prose does not substitute: measured, such a thread spawned a *subagent* which
hand-rolled a `git worktree add` into `/tmp` on a **detached HEAD**, leaving
Codex's managed root untouched. You get a directory, not a managed worktree: no
branch to push, nothing Codex tracks or restores, and nothing the handoff flow
can bring back.

So:

| You want | Do |
| --- | --- |
| Parallel work in Codex's own worktrees | Ask the user to start it in Codex Desktop (worktree mode, or `/fork` into a worktree). codex-pilot then drives the resulting threads normally — they are app-owned and list with the worktree as their `cwd`. |
| Parallel work you drive yourself | `start_thread(repo=..., branch=...)` per slice: it makes the worktree in Codex's own root and starts the thread in it. |

Two rules that follow:

- **Do not park unmerged work in a worktree**, whoever made it. Codex clears that
  root to reclaim space and says so only afterwards. Commit and push the branch,
  or bring the changes back through the app's handoff.
- **Do not hand-roll `git worktree add`** when `start_thread` will do it. The
  hand-rolled attempt observed here landed in `/tmp` on a detached HEAD, which is
  work with no branch to push.

## Orchestrating several threads

The loop is start, follow, wait once, harvest.

1. **A worktree per slice, then `start_thread` in each** — one you made, not one
   under Codex's worktree root (see *Parallel work* above). Keep the returned
   thread ids; they are your handle on the work. If the slices should instead run
   in Codex's own worktrees, that is the user's move in the app, and you drive the
   threads it produces.
2. **`follow_thread` the ones the app has mounted** — `sync_threads` tells you
   which those are, and mounts the rest if you ask. A follow is a subscription to
   the app's stream, so it only streams for a thread Codex Desktop is rendering.
   You do *not* need it for `start_thread` runs: those are watched as processes
   and report on their own.
3. **Wait once, not per thread.** One `collect_events(wait_seconds=60)` covers
   every followed thread and every detached run, across every instance. Pass the
   previous `cursor` so you only see what is new; a non-zero `dropped` means
   re-read state rather than trusting the list.
4. **`turn_completed` means that thread is free.** For a detached run its `data`
   carries `route: "detached"` and the exit code; `run_failed` means it exited
   non-zero, so read `log_path` before assuming the work happened — unless its
   `stopped` is true, which means you stopped it yourself.
5. **Harvest with `read_thread`**, not by re-running the agent. It works
   whether or not the app ever mounted the thread. (`rollout` from
   `thread_status`/`list_threads` is the same file if you want it raw, and
   `log_path` is the JSONL for a run started here.)

Do not poll `thread_status` in a loop. Nothing pushes into a Claude Code
session, so you do have to *call* `collect_events` — but events accumulate while
you are away, so one call after doing something else returns the whole backlog.

### Supervising a thread

**Approvals only reach you on a thread the app owns.** A detached run has no way
to ask a human, which is why `start_thread` defaults to `approval="never"`. So
ordering matters, and the obvious order is wrong:

    start_thread(...)                       # runs unattended, holds the lock
    → wait for turn_completed
    → focus_thread                          # now the app owns it
    → edit_thread(..., {"approvalsReviewer": "user"})
    → send_message(...)                     # this turn is supervised

`edit_thread` needs the app to own the thread, so it cannot be used on a
freshly started one — the run itself holds the lock. If a slice needs approvals
from its very first turn, create the thread in Codex Desktop instead and drive
it over IPC from the start.

### While a detached run is going

Its route reads `detached_running`, and neither driving route works: `steer_turn`,
`respond`, `edit_thread` and `focus_thread` all refuse, by design. What you can
do is wait for it, read `log_path`, or `stop_turn` to terminate it and its whole
process group.

**Never `focus_thread` a running one.** That asks the app to open a thread our
own writer still holds, and two writers on one rollout corrupt it. The tools
refuse it for you, but do not go looking for a way around.

Scale down before scaling up. Fan out when the slices are genuinely independent
and each has its own worktree; otherwise one thread working in order beats
reconciling three that edited the same files.
