---
name: driving-codex-desktop
description: Drive Codex Desktop threads - send work, steer a running turn, stop one, answer approval requests, change model/reasoning/plan mode, set goals. Use when supervising or orchestrating Codex agents from Claude Code, or when a codex-pilot tool returns something unexpected.
---

# Driving Codex Desktop threads

`codex-pilot` drives real agents doing real work. A steer lands mid-thought; a
stop discards what a turn had in flight; an approval runs a command. Read
`thread_status` before acting on a thread you did not just act on.

## Which verb

| Situation | Use |
| --- | --- |
| Thread is idle, you have new work | `send_message` |
| Turn is running and heading the wrong way | `steer_turn` |
| Turn is running and should stop | `stop_turn` |
| Thread is waiting on an approval or a question | `respond` |
| Change model, reasoning, plan mode, fast mode, sandbox | `edit_thread` (`update_settings`) |
| Give it a standing objective across turns | `set_goal` |
| Context is getting long | `edit_thread` (`compact`) |

`send_message` on a busy thread queues behind the running turn rather than
interrupting it. To change what the *current* turn is doing, steer.

## The writer lock decides the route

Codex allows one writer per thread, and `send_message` picks accordingly:

- **App has the thread open** → IPC. Everything works: steer, stop, respond.
- **Nothing holds it** → resumed detached. Returns a `pid` and `log_path`
  immediately and runs in the background. Steer and stop are *not* available;
  poll the log. An archived thread is unarchived first, reported as
  `unarchived: true`.

Never try to defeat the lock. Two writers on one rollout corrupt it. If a tool
reports a `DriftError` — lock held but no window claims the thread — that is a
Codex Desktop update having moved a protocol version, not a thread you can grab.
Run `scripts/extract_registry.py --check` and re-pin.

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

`thread_status` returns `state: null` with a `note` when it could not read.
**That is not "nothing is pending."** Treat a null state as unknown and look at
the app before concluding a thread is idle or unblocked.

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
- `approvalPolicy`, `approvalsReviewer`, `sandboxPolicy`, `permissions`
- `multiAgentMode`, `cwd`

## Several Codex apps

Each install has its own `CODEX_HOME`, and **thread ids are unique only within
one**. `list_threads` tags every thread with its instance. If a name exists in
two, the tool refuses rather than guessing — pass `instance` to choose.

## Starting new threads

`codex-pilot` drives existing threads. To create one, use the separate `codex`
MCP server (`codex mcp-server`), or `codex exec` in the target directory.
