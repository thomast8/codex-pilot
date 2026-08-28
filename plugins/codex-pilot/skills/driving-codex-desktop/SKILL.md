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
| A new parcel of work | `start_thread` (never `codex exec`), even if an idle thread could take it |
| More of the parcel a thread is already on | `send_message` |
| A turn is running and you have not looked at it | `read_thread` |
| Turn is running and heading the wrong way | `steer_turn` |
| Turn is running and should stop | `stop_turn` |
| Thread is waiting on an approval or a question | `respond` |
| Change model, reasoning, plan mode, fast mode, sandbox | `edit_thread` (`update_settings`) |
| Give it a standing objective across turns | `set_goal` |
| Reusing one thread across parcels | `edit_thread` (`compact`) between them |
| A thread's parcel is finished and harvested | archive it — in the app, or `codex archive` |
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
do something else, look in on it while it is still running, and collect the
result when the thread reports idle.

## Starting new threads

`start_thread(text, cwd=...)`. It creates the thread, starts its first turn, and
returns the new thread id in about a second while the agent keeps working.

**Do not use `codex exec` from Bash, and do not use the standalone `codex` MCP
server, to start Codex work.** Both block until the agent finishes, so the
session sits idle for however long the task takes, and both inherit whatever
directory they were called from. `start_thread` has neither problem, and the
thread it creates is drivable by every other tool here.

Name `model`, `effort` and `service_tier` on the call when the work deserves
something other than the machine's defaults. Left out, each is inherited from the
user's `~/.codex/config.toml` — which Codex Desktop rewrites, so what you inherit
drifts. Passing them is also the *only* way to set them for a detached run:
see *Never edit `~/.codex/config.toml` to set up a turn* below.

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

### One thread per parcel of work

**Prefer a new thread for a new parcel, even when an idle one could take it.**
`send_message` is for continuing what a thread is already doing: a fix to what it
just built, a question about its own output, the next step of the same slice.
Work that is not that gets `start_thread`.

The reason is context, and it does not announce itself. A thread carries
everything it has done, so a fresh task sent to an old one starts out crowded
with a previous task's files, dead ends and decisions, and it inherits that
thread's `cwd`, worktree and branch whether or not the new work belongs there.
A new thread starts clean, in the directory the work actually belongs to, and
runs alongside the old one instead of queueing behind it.

**When you do keep one thread across parcels, compact between them:**

    edit_thread(thread, "compact")

Do it at a seam: the thread idle, one parcel finished, the next not yet sent.
Compact goes over IPC, so the app has to own the thread and be showing it, and a
detached run cannot be compacted at all until the app has it (`focus_thread`
once it is idle). Nothing here reports how full a thread's context is, so this
has to be a habit tied to the shape of the work rather than a reaction to a
gauge that does not exist.

### Archive a thread when its parcel is done

A thread per parcel means threads accumulate, and they are not free while they
sit there. The app holds a writer lock on **every** thread it has open and
mounts only a few of them, so each finished thread left open is one more
lock-holder that neither route can drive until something surfaces it — the pile
that makes `sync_threads` expensive and every `focus_thread` a flash. Archiving
takes a thread out of that set and releases its lock.

Nothing is lost by it. The rollout moves to `archived_sessions/`, `read_thread`
reads it there exactly as before, and the thread's `route` becomes `detached`
because nothing holds it any more. It is not one-way either: `send_message`
unarchives on the way through and reports `unarchived: true`, resuming the
thread detached. So archive after you have harvested, not instead of harvesting.

**"This task is archived" on screen is not evidence that it is.** That wall is
gated by the window's own navigation state, not by the thread: decoded from the
bundle, an `archivedConversationPreview` flag on the route renders it, and the
app has an explicit handler that clears the flag — so it can outlive the
navigation that set it and greet a perfectly live thread. Measured 2026-08-28 on
a thread showing that screen: its rollout was under `sessions/`, the app's own
live listing for its cwd contained it, and all 390 archived rows did not. It
also kept answering owner discovery as mounted the whole time. So do not read
that screen as a fact about the thread, and do not read a focus that lands on it
as a failed focus. Clicking any other chat clears it.

Archived-ness itself is a single state seen from two sides, and they agree: a
thread the app lists as archived has its rollout under `archived_sessions/`,
which is exactly what `archived` on `thread_status` reports. If you want the
app's own list rather than the disk's, the app-server answers it over the same
probe used for the model catalogue: `thread/list` with `{"archived": true}`.

**codex-pilot has no archive verb; there are two routes and the lock decides
which.**

- **The app holds it** → archive it in Codex Desktop. The app releases the
  thread as it archives, which is why this works while the CLI route does not.
- **Nothing holds it** (a `start_thread` run that has exited, route `detached`)
  → the instance's own binary:

      CODEX_HOME=<that instance's home> "<App>.app/Contents/Resources/codex" archive <thread-id>

  Bind both halves to one instance, as with every other shell-out here. Against
  a thread the app has open it fails with `failed to archive session`, which is
  the lock talking.

  `<App>` is not always the obvious one: several bundles can claim a home and
  only one is running it. Take the bundle of whatever is listening on
  `<home>/ipc/ipc.sock`, and when nothing is, the bundle stamped with that home
  — for `~/.codex` the *unstamped* `/Applications/ChatGPT.app`, since a clone
  may stamp it as well.

Archive threads *you* started, once their work is harvested and their branch is
committed and pushed — archiving does not preserve a worktree, and Codex clears
its own worktree root without warning. Never archive a thread the user has open
in front of them: that is their window, not your housekeeping.

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

- **A detached `codex exec` holds it** (`route: detached_running`) → neither
  driving route works until it exits. `started_here: true` means the run is
  ours: wait for `turn_completed`, read its `log_path`, or `stop_turn` it. With
  no `started_here` the writer belongs to another process — another Claude
  session's codex-pilot, or a terminal — and then only waiting is available.
  `stop_turn` refuses it rather than signalling a process group that is not
  ours; `read_thread` still shows what it is doing. Do not focus either into
  the app meanwhile.

- **App holds it but is not *showing* it** → neither route works. The app locks
  every thread it has open but only answers for the one a window is rendering,
  so this is common. `UnclaimedThreadError` says so; call `focus_thread`, wait a
  couple of seconds, and retry. The link is aimed at the app actually serving
  that instance, so a focus that does no good is no longer the "wrong app on a
  multi-install machine" case it used to be. If the window came forward showing
  "This task is archived", that is the navigation-state wall above and not a
  failed focus. Only past both is protocol drift after an app update worth
  suspecting, which `scripts/extract_registry.py --check` answers.

- **The lock state could not be established** (`route: unknown`, or
  `lock_known: false`) → the holder could not be classified, or `lsof` could not
  be run at all. Not a synonym for free: `send_message` will try IPC, which is
  harmless when wrong, and refuses to resume, which is not.

Which case you are in is on `thread_status` and every `list_threads` row:
`holder` names the process holding the lock, and `route` says what that makes
possible. Both are read from the process table rather than from anything only
the calling process remembers, so a second session sees the same answer.

Never try to defeat the lock. Two writers on one rollout corrupt it.

### "This is open in another app"

Codex Desktop showing that banner over a thread, with nothing streaming into
the window, is this state seen from the other side: some other writer holds the
lock, so the app cannot attach and shows a static read of the rollout. When the
writer is a `codex exec` run, that is `route: detached_running`, and it is
working normally — the turn is being appended to the rollout the whole time.
Do not press Retry to force it. Read `log_path` (if the run is ours) or the
rollout; the app renders the whole turn once the writer exits.

### Focusing takes the screen, and hands it back

`focus_thread` reports the bundle it aimed at on `app`. On a machine with more
than one Codex install that is worth reading: `codex://` is claimed by every
bundle and macOS picks one handler, so an unaimed link can land in an app whose
`CODEX_HOME` has never heard of the thread and say nothing about it. If no app
is serving the instance and none can be named for it, the call refuses instead
of firing a link that would go somewhere arbitrary — launch that instance's
Codex Desktop and retry.

`focus_thread` raises Codex Desktop over whatever the user is working in. That
part is not ours to prevent: the deep link is the app's own navigation route,
and handling one runs `ensurePrimaryWindowVisible` — restore, show, focus on the
primary window — *before* it navigates. `-g`, which is what `activate=false`
does, only stops macOS activating the app on the launch side.

What the plugin does instead is give the screen back. It notes the frontmost app,
fires the link, waits for the Codex window it aimed at to actually come forward,
and reactivates what was displaced; `sync_threads` does it once for the whole
sweep rather than per thread. Because the link names one bundle, a user sitting
in a *different* Codex instance counts as displaced and gets put back, rather
than being read as already where they were being sent. The result is on `focus`
in both return values:
`{"restored": true, "app": "..."}`, or `restored: false` with a reason, which is
the honest half of the feature —

- `already_frontmost` — the user was in Codex to begin with; nothing was taken.
- `not_raised` — no Codex window came forward within a few seconds, or the user
  moved on to something else meanwhile. Reactivating on that basis would drag
  them out of wherever they went, so it does not. Note the third case this
  covers: a cold or busy app that raises *after* the guard stopped watching,
  where the interruption still lands and nothing undoes it.
- `not_confirmed` — the reactivation was issued and the app never came back to
  the front. Running the command is not the same as winning the foreground, and
  this says which happened.
- `frontmost_unknown` / `activate_failed` — the probe or the
  reactivation failed outright. The screen is wherever the app left it.

So this turns being yanked away into a flash, which is better and is still an
interruption. Focus deliberately, and rarely:

- **Never focus just to look.** `read_thread` answers "what is it doing" off
  disk, for any thread, mounted or not, without touching the app. Focusing is
  for when you need to *drive* the thread over IPC: steer, respond,
  `edit_thread`, compact, or a follow that must stream.
- **Focus once, in a batch, for the threads you actually intend to drive.**
  Mounting is additive, and `sync_threads` restores the screen once for the
  whole sweep, so mounting five threads together costs one flash — the same
  five focused one at a time as you get to them cost five.
- **`sync_threads(mount=true)` focuses every unmounted thread it can**, and
  with no `threads` argument that is all of them. Name the threads you mean.
  The ones it could not mount come back in `skipped` with a reason rather than
  failing the sweep: `unresolvable` (listed off a writer lock, but with nothing
  on disk to bind it to yet — often a thread the app has only just made, and
  worth retrying), `detached_running` (one of our own runs holds it),
  `refused` (someone else's writer does) and `unaimable` (its instance's
  serving app could not be named, so there is no link to aim). Nothing in
  `skipped` was mounted.
- If it is not worth interrupting the user for, it is not worth focusing for.
  Read the rollout and leave the app where it is. The restore is a mitigation,
  not a licence.

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
raises the app's window and hands the screen back after, see below) or a look
at the app.
`disk.phase: "idle"` means it finished its last turn, so a null state there is
much less interesting. `disk: null` means even that was unreadable.

The rollout never carries the pending request itself — Codex writes no record for
one — so a disk block is evidence about liveness, never about approvals.

Following a thread has the same rule. `collect_events` reports each thread's
`health`: `ok`, `resyncing` (asked, nothing back yet — including a follow still
waiting on its first snapshot, which is what an unmounted thread looks like),
`lost` (the connection dropped), `detached` (a run started here; no live state,
but its completion still arrives as an event), or `not_following` (never
followed, or lost with the server process).
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

- `model`, `effort` (reasoning), `personality`, `summary` — see *Models and
  reasoning effort* below before naming a value for either
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

### Models and reasoning effort

**Never assert from memory which models exist or how high the effort ladder
goes.** Both come from a server-side catalogue that changes with releases, and
each model advertises its own ladder, so anything written down here is a
snapshot and yours may already differ. Saying "that isn't a real level" without
looking is how a thread ends up parked one rung below what the work needed.

Ask the instance instead. Its own `codex` binary answers, and this is a read:

```sh
{ printf '%s\n' \
  '{"id":1,"method":"initialize","params":{"clientInfo":{"name":"probe","title":"probe","version":"0"}}}' \
  '{"id":2,"method":"model/list","params":{"includeHidden":true}}'; sleep 5; } \
  | CODEX_HOME=<that instance's home> "<App>.app/Contents/Resources/codex" app-server
```

Keep stdin open, hence the `sleep`; the server answers and a closed pipe cuts it
off first. Bind both halves to one instance, as everywhere else here: its own
bundle rather than the `codex` on PATH, which is usually an older build (picking
`<App>` is the same problem as under archiving above), *and*
its `CODEX_HOME`, since the catalogue follows the account that home is signed
into. Run bare against a second instance you get the default home's answer, with
no error to tell you. Each entry gives `model`,
`displayName`, `defaultReasoningEffort`, `supportedReasoningEfforts` (with a
sentence describing each rung), `serviceTiers` and `hidden`.

What is stable enough to rely on is the shape:

- The ladder runs **low → medium → high → xhigh → max → ultra**, and how far it
  goes is per model. `ultra` is not a synonym for `max`: it is described as
  maximum reasoning *with automatic task delegation*, and on this machine only
  the newest multi-agent models offered it, while several older ones stopped at
  `xhigh`. (The app's own settings enum also carries `none` and `minimal`, but
  no model in the catalogue advertised them.)
- Defaults differ per model, and a thread you did not configure is not
  necessarily on the default anyway: threads inherit `model`,
  `model_reasoning_effort` and `service_tier` from the user's
  `~/.codex/config.toml`. Read `thread_status` to see what a thread is actually
  on rather than assuming.
- The picker in the app shows a *subset*, controlled by a setting, so a rung
  missing from the UI can still be a valid value to send.

`docs/protocol.md` holds a dated reading of the catalogue and the query's full
output shape. It is there to show what the answer looks like, not to save you
the call.

Set effort and model to the work, deliberately, rather than leaving whatever was
inherited: a defect brief or production code is worth `xhigh` or `max`, a
mechanical edit is not. After setting, read `thread_status` back on the next turn
to confirm it took; an effort a model does not support has no promised behaviour.

### Never edit `~/.codex/config.toml` to set up a turn

That file is Codex Desktop's, and the app rewrites it — including keys you put
there. Restoring `model_reasoning_effort` or `service_tier` in it before
dispatching work is a trap that looks like diligence: it races the app, it is
undone the next time the app writes, and until then it has moved every other
thread and every interactive session onto settings they never asked for. Reading
it is fine. Writing it is not yours to do, and a run that seems to need it is a
sign you are reaching for the wrong lever.

There are two right levers, one per route:

- **Dispatching detached** (`start_thread`, and `send_message` on a thread no
  window owns) — pass `model`, `effort` and `service_tier` on the call. They
  become `-c` overrides on that one `codex exec`, so nothing outside the turn
  moves. Each detached turn is its own process, so pass them again per call;
  they do not carry over.
- **A thread the app has open** — `edit_thread(action='update_settings')`, which
  lands on the next turn. `send_message` refuses these arguments on that route
  rather than dropping them, because the IPC turn carries no settings and would
  otherwise run at the old ones while you believed otherwise.

Two things neither lever will tell you, so check rather than assume:

- **A bogus effort does not fail.** The CLI accepts any string
  (`reasoning effort: bogus` in its own header) and dispatches. Read the rung
  back from `thread_status`, or from the rollout's `turn_context`.
- **An unsupported service tier is dropped, and the turn still runs.** A tier a
  model does not advertise comes back as an error *item* in the run's JSONL —
  "will be omitted from requests" — and then the turn proceeds at the default.
  That item in `log_path` is the only warning; the tier is not recorded in the
  rollout for a detached run, so there is nothing to read back afterwards.

### Changing settings on a thread that is already working

`update_settings` lands on the **next** turn, so there are two ways to move a
running turn onto a different effort, model or service tier, and steering is not
one of them: `steer_turn` sends text and nothing else, joining the turn already
under way with the settings it started on. (The app's own steer signature does
carry a `serviceTier` argument, decoded from the bundle and never exercised;
codex-pilot does not send one, so do not plan around it.)

- **Wait for the boundary.** Let `turn_completed` arrive, then
  `edit_thread(..., "update_settings", {...})`, then `send_message` the next
  piece of work. Free, and the default.
- **Cut the turn short.** `stop_turn`, then `update_settings`, then
  `send_message("continue where you left off: ...")`. The new turn runs under
  the new settings. This costs whatever the stopped turn had in flight and had
  not yet written, so it is worth it when a turn is visibly under-powered for
  what it turned out to be — not as a routine way to fiddle with settings.

Say which one you are doing and why, since the second one throws work away.

Both routes need the app to own the thread, because `update_settings` is an IPC
verb. A thread nothing owns has a simpler answer: pass `model`, `effort` or
`service_tier` straight to the next `send_message`, which is a fresh `codex exec`
and takes them as overrides for that turn. `focus_thread` only to pay the
foreground interruption for something the detached route genuinely cannot do.

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
wait. On a turn you mean to supervise, make that watch a timed one that wakes
you partway through (`--timeout`, no `--until`); `--until turn_completed` parks
you until the end, which is right only for a turn you are content to leave
unattended.

A follow only streams while the app has the thread **mounted**. If a follow stays
silent on an app-owned thread, `focus_thread` and retry.

Runs started by `start_thread` need no follow: they are watched as processes, and
their exit arrives on the same stream as `turn_completed` (or `run_failed`), so
one `collect_events` covers both kinds.

## Watch the work, do not just wait for the result

A turn heading the wrong way is cheap to fix in its first minute and expensive in
its twentieth. So look in on any turn whose direction matters while it is still
running. Waiting for `turn_completed` and reading the result is what you do with
a turn you were happy to let run unattended, not the default for everything.

It takes two sources, because neither is enough alone:

- **Events say *when*.** `collect_events` reports turn boundaries and approval
  requests and carries no content whatsoever, so it can never tell you a turn is
  going wrong. Silence from it means "still running", not "still on track".
- **`read_thread` says *what*.** The rollout gains each item as the turn
  completes it, so a read mid-turn shows the messages and tool calls so far. It
  reads disk and never goes near the app, which makes it the safe probe: it
  works on every route, running or not, and cannot disturb the writer. For a run
  `start_thread` made, `log_path` is the same story as JSONL.

Make it a deliberate check-in, not a poll. On a turn expected to run for minutes,
read it once it has had time to commit to an approach, then again if it is still
going, doing other work in between. The anti-polling rules elsewhere here are
about hammering the app for state; reading the rollout is not that.

If you have nothing else to do meanwhile, background a watch with a `--timeout`
shorter than the turn and no `--until`: it streams events, then exits 0 on the
timeout, which wakes you for the look-in instead of parking you until the thread
is finished.

Then act on what you saw:

- **App-owned thread going wrong** → `steer_turn`. It lands in the running turn,
  so the correction arrives while the work is still cheap to redo. This is the
  whole reason to look.
- **Detached run going wrong** → it cannot be steered. Either `stop_turn` it and
  start the work again with instructions that rule out what it just did, or let
  it finish and correct on the next turn. Decide which; do not drift into the
  second by default.
- **Wrong from the first instruction rather than drifting** → stop it and rewrite
  the task. A steer that fights a bad brief loses.
- **On track** → leave it alone. A steer that only restates the task interrupts a
  working turn for nothing.

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
you which are mounted, and with `mount=true` brings forward every one of the
rest it can — check `skipped` for the ones it could not, and why.

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

The loop is start, follow, wait once, look in, harvest.

1. **A worktree per slice, then `start_thread` in each** — one you made, not one
   under Codex's worktree root (see *Parallel work* above). Keep the returned
   thread ids; they are your handle on the work. If the slices should instead run
   in Codex's own worktrees, that is the user's move in the app, and you drive the
   threads it produces.
2. **`follow_thread` the ones the app has mounted** — `sync_threads` tells you
   which those are, and mounts the rest if you ask, at the cost of one flash of
   the Codex window for the sweep, so name the ones you will actually drive. A
   follow is a subscription to the app's stream, so it only streams for a thread
   Codex Desktop is rendering.
   You do *not* need it for `start_thread` runs: those are watched as processes
   and report on their own.
3. **Wait once, not per thread.** One `collect_events(wait_seconds=60)` covers
   every followed thread and every detached run, across every instance. Pass the
   previous `cursor` so you only see what is new; a non-zero `dropped` means
   re-read state rather than trusting the list.
4. **Look in on the long ones before they finish.** One `read_thread` per
   thread that has been running a while shows what each is actually doing, off
   disk, without touching the app or any thread's writer. Steer the app-owned
   ones that are drifting; on a detached run, stop it and restart the slice with
   a better brief rather than harvesting a wrong answer later.
5. **`turn_completed` means that thread is free.** For a detached run its `data`
   carries `route: "detached"` and the exit code; `run_failed` means it exited
   non-zero, so read `log_path` before assuming the work happened — unless its
   `stopped` is true, which means you stopped it yourself.
6. **Harvest with `read_thread`**, not by re-running the agent. It works
   whether or not the app ever mounted the thread. (`rollout` from
   `thread_status`/`list_threads` is the same file if you want it raw, and
   `log_path` is the JSONL for a run started here.)
7. **Archive what you harvested**, once its branch is pushed. Threads you
   started otherwise stay in the app's open set, holding locks and crowding the
   mount problem every later step has to work around.

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
process group. That last one only for a run this server started (`started_here`):
another process's writer gets the same refusal as every other verb.

**Never `focus_thread` a running one.** That asks the app to open a thread our
own writer still holds, and two writers on one rollout corrupt it. The tools
refuse it for you, but do not go looking for a way around.

Scale down before scaling up. Fan out when the slices are genuinely independent
and each has its own worktree; otherwise one thread working in order beats
reconciling three that edited the same files.
