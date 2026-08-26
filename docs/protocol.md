# Codex Desktop IPC protocol

Decoded from the installed app, not from documentation. Two sources, in order of
authority:

> **Use the app's own codex binary as the schema source, not the CLI on your
> PATH.** They are different builds — here `/Applications/ChatGPT.app/Contents/
> Resources/codex` is `0.149.0-alpha.4.3` while `codex` on PATH is `0.147.0`.
> The newer build adds a whole queue API (`thread/queue/add|delete|list|reorder|
> start|update`) and `thread/revert` that the CLI's schema does not mention.

1. **The installed app bundle** — `/Applications/ChatGPT.app/Contents/Resources/app.asar`.
   Two files inside it matter: `/.vite/build/src-DlBR1tzg.js` (Electron main
   process, contains the router and the framing) and
   `/webview/assets/app-initial-q5My48Y-.js` (renderer, contains the request
   handlers and their param destructuring). **This is the source of truth.**
2. **The app-server JSON schema** — `codex app-server generate-json-schema --experimental --out DIR`.
   Authoritative for the value types the app-server itself accepts (decision
   enums, `UserInput`), which the IPC layer passes through.

[remodex](https://github.com/Emanuele-web04/remodex) is corroborating evidence
only. Its framing and version map agree with ours, but its **payload shapes do
not** — it targets a different app build (see [Divergences](#divergences)).

Everything marked **verified** was executed against the running app. Everything
marked **decoded** was read out of the bundle and not yet executed.

## Transport

The Electron main process listens on a unix stream socket:

    $CODEX_HOME/ipc/ipc.sock

with a fallback candidate at `$TMPDIR/codex-ipc/ipc-<uid>.sock` (bundle function
`ece`; not present on this machine). `CODEX_HOME` defaults to `~/.codex`.

**Multi-instance:** every path this tool touches lives under `CODEX_HOME` — the
socket, `session_index.jsonl`, `thread-writer-locks/`, `sessions/`,
`archived_sessions/`. Doppel gives each cloned ChatGPT app its own by stamping
`LSEnvironment.CODEX_HOME` into the bundle's Info.plist, so instance targeting is
just picking a `CODEX_HOME`. **Verified:** handshakes succeed against both
`~/.codex` (ChatGPT.app) and `~/.codex-secondary` (ChatGPT Personal.app),
returning distinct client ids. Thread ids are unique *per instance*, so a bare
thread id is ambiguous across two.

### Framing — verified

    [4 bytes: uint32 little-endian body length][body: UTF-8 JSON]

Writer is bundle function `g9`; reader is `m9`. The reader destroys the socket on
a length of `0` or above `268435456`. The length counts **UTF-8 bytes**, not
characters.

Newline-delimited JSON is not valid here. The reader reads the first four bytes
as a length, gets nonsense, and closes the connection — which looks exactly like
a rejected handshake.

## Router

The main process is an `IpcRouter` (bundle class `ice`) that routes between
connected clients. Envelope types: `broadcast`, `request`, `response`,
`client-discovery-request`, `client-discovery-response`.

### Handshake — verified

```json
{"type":"request","requestId":"<uuid>","method":"initialize",
 "params":{"clientType":"codex-pilot"},"version":1}
```

replies

```json
{"type":"response","requestId":"<uuid>","resultType":"success","method":"initialize",
 "handledByClientId":"<uuid>","result":{"clientId":"<uuid>"}}
```

### Routing — verified

For any non-`initialize` request the router calls `findClientForRequest`:

- with `targetClientId`, it asks only that client;
- without, it asks every other client and takes the first to accept
  (`Promise.any`).

It asks by sending each candidate a `client-discovery-request`; the candidate
answers `client-discovery-response` with `{canHandle: bool}`. If nobody accepts,
the caller gets `{"resultType":"error","error":"no-client-found"}`. The router's
discovery timeout is 10s (`v9 = 1e4`), so a client timeout should exceed that.

You are a client on this bus too: the router will ask *you* to handle other
clients' requests. Decline them.

### The version gate — verified

**Every request must carry a `version` matching the method's pinned version.** A
client receiving an unrecognised version answers `canHandle: false`, so the
caller sees `no-client-found` — indistinguishable from "nobody owns this thread"
unless you know to look for it.

Confirmed with a negative and a positive control on the same request:
`thread-owner-discovery` with no `version` returned `no-client-found`; identical
request with `"version": 1` returned `resultType: "success"` from the real owner.

Version map, verbatim from the bundle's `b_` object (remodex's
`DESKTOP_IPC_METHOD_VERSIONS` agrees — use the disagreement as a drift alarm):

| Method | Version |
| --- | --- |
| `thread-stream-state-changed` | 11 |
| `thread-stream-following-changed` | 1 |
| `thread-stream-following-status-requested` | 1 |
| `ipc-connection-reset` | 1 |
| `thread-read-state-changed` | 2 |
| `thread-archived` | 2 |
| `thread-unarchived` | 1 |
| `thread-owner-discovery` | 1 |
| `thread-follower-start-turn` | 2 |
| `thread-follower-load-complete-history` | 1 |
| `thread-follower-compact-thread` | 1 |
| `thread-follower-steer-turn` | 1 |
| `thread-follower-interrupt-turn` | 4 (3 — see below) |
| `thread-follower-update-thread-settings` | 1 |
| `thread-follower-edit-last-user-turn` | 2 |
| `thread-follower-command-approval-decision` | 1 |
| `thread-follower-file-approval-decision` | 1 |
| `thread-follower-permissions-request-approval-response` | 1 |
| `thread-follower-submit-user-input` | 1 |
| `thread-follower-submit-mcp-server-elicitation-response` | 1 |
| `thread-follower-set-queued-follow-ups-state` | 1 |
| `thread-queued-followups-changed` | 1 |

`initialize` is handled by the router itself and is not in the map; it takes
version 1.

**Interrupt is the one special case** (bundle function `S_`): it is version **3**
when `expectedTurnId` is absent or null, and version **4** when present. So the
precondition-free form stays callable on a client that only speaks v3.

## Acting on a thread

Two steps. Owner discovery first:

```json
{"type":"request","requestId":"...","method":"thread-owner-discovery",
 "params":{"hostId":"local","conversationId":"<thread uuid>"},"version":1}
```

The response's `handledByClientId` is the owning client. Then send the
`thread-follower-*` request with `targetClientId` set to it.

Every follower handler begins with `assertThreadFollowerOwner(params.conversationId)`,
so a misrouted request is rejected by the app rather than silently applied to the
wrong thread.

### Handler signatures — decoded

From the renderer's dispatch switch (function `Kot`):

| Method | Renderer call |
| --- | --- |
| `-interrupt-turn` | `interruptConversation(conversationId, mode, expectedTurnId)` → `{interruptedTurnId, ok}` |
| `-steer-turn` | `steerTurn(conversationId, input, restoreMessage, serviceTier, attachments, clientUserMessageId, additionalContext)` |
| `-start-turn` | `startTurn(conversationId, turnStart)` |
| `-compact-thread` | `compactThread(conversationId)` |
| `-edit-last-user-turn` | `editLastUserTurn(conversationId, params)` |
| `-load-complete-history` | → `{revision}` |
| `-update-thread-settings` | `updateThreadSettingsForNextTurn(conversationId, threadSettings)` |
| `-command-approval-decision` | `replyWithCommandExecutionApprovalDecision(conversationId, requestId, decision)` |
| `-file-approval-decision` | `replyWithFileChangeApprovalDecision(conversationId, requestId, decision)` |
| `-permissions-request-approval-response` | `replyWithPermissionsRequestApprovalResponse(conversationId, requestId, response)` |
| `-submit-user-input` | `replyWithUserInputResponse(conversationId, requestId, response)` |
| `-submit-mcp-server-elicitation-response` | `replyWithMcpServerElicitationResponse(conversationId, requestId, response)` |

`interrupt` `mode` values seen in the bundle: `"user-stop"` (the Stop button) and
`"descendant-cleanup"` (used when tearing down subagents). On a goal-pause the
handler returns `{interruptedTurnId, goalPauseError, ok}` instead of throwing.

> **`ok` is not the result of an interrupt** — verified live. The app answers
> `{"ok": true, "interruptedTurnId": null}` both when the thread was already
> idle and when an `expectedTurnId` precondition did not match the running turn;
> in the latter case the turn keeps running. `interruptedTurnId` is the only
> signal that something was actually stopped. The precondition fails softly:
> there is no error to catch.

`steer` needs `restoreMessage` — also verified live, by its absence. The renderer
reads `restoreMessage.cwd` and `restoreMessage.context.workspaceRoots` with no
guard, so omitting it surfaces as `Cannot read properties of undefined (reading
'cwd')` from inside the app rather than as a protocol error. `cwd` may be null
(the app falls back to the conversation's own); the object and its `context` may
not be absent.

`start-turn`'s `request` must repeat the conversation id as `threadId`: the
renderer throws `Turn request thread does not match the conversation` otherwise.
It returns the new `{turn: {id, status}}`, so the turn id needed for a later
`expectedTurnId` comes back from the send itself — no need to read the rollout.

`steer` `input` is an **array** of `UserInput`; the text variant is
`{"type":"text","text":"...","text_elements":[]}`. Two independent sources agree:
the app-server schema types `TurnSteerParams.input` as an array, and remodex
sends `Array.isArray(params.input) ? params.input : []`.

## Pending approvals — decoded

Pending requests live on the conversation object as `conversation.requests`, each
`{id, method}`. The bundle's bulk-decline routine (`dot`) enumerates every kind:

| `request.method` | Reply with | Decline value used by the app |
| --- | --- | --- |
| `item/commandExecution/requestApproval` | `-command-approval-decision` | `"decline"` |
| `item/fileChange/requestApproval` | `-file-approval-decision` | `"decline"` |
| `item/permissions/requestApproval` | `-permissions-request-approval-response` | `{"permissions":{},"scope":"turn"}` |
| `item/tool/requestUserInput` | `-submit-user-input` | `{"answers":{}}` |
| `item/tool/requestOptionPicker` | (option picker) | `{"action":"dismiss","selectedOptions":[],"freeformAnswer":null}` |
| `item/tool/requestSetupCodexContextPicker` | (context picker) | `{"action":"dismiss","selectedSources":[]}` |
| `mcpServer/elicitation/request` | `-submit-mcp-server-elicitation-response` | `{"action":"decline"}` |

### Decision values — from the app-server schema

**`CommandExecutionApprovalDecision`:**

| Value | Meaning |
| --- | --- |
| `"accept"` | approve this command |
| `"acceptForSession"` | approve **and stop prompting** for matching commands this session |
| `{"acceptWithExecpolicyAmendment":{"execpolicy_amendment":[...]}}` | approve and persist an execpolicy rule |
| `{"applyNetworkPolicyAmendment":{...}}` | persist an allow/deny network rule for a host |
| `"decline"` | deny; the agent continues the turn |
| `"cancel"` | deny **and interrupt the turn** |

**`FileChangeApprovalDecision`:** `"accept"`, `"acceptForSession"`, `"decline"`
(agent continues), `"cancel"` (denies and interrupts).

**`McpServerElicitationAction`:** `"accept"`, `"decline"`, `"cancel"`.

> The four values beyond plain accept/decline are **persistent grants**, not
> per-request answers. `acceptForSession` disables prompting for matching
> commands for the rest of the session; the two amendment forms write policy that
> outlives the turn. A caller answering approvals automatically should stick to
> the single-shot `accept` / `decline` / `cancel` and treat the rest as
> deliberate, separately-authorised acts.

## Observing a thread

`thread-stream-state-changed` (v11) is broadcast with an explicit
`targetClientIds` list, so you receive it only as a registered follower.

To follow, broadcast:

```json
{"type":"broadcast","method":"thread-stream-following-changed",
 "params":{"conversationId":"...","hostId":"local","following":true},"version":1}
```

Owners then send `thread-stream-state-changed` carrying
`change: {type: "snapshot" | "patches", baseRevision, revision, patches}`.
Broadcasting `thread-stream-following-status-requested` (v1) prompts owners to
re-announce. Set `following: false` to stop.

**Verified:** the follow handshake works. Broadcasting `following: true` yields a
`change: {type: "snapshot", revision: 1, conversationState: {...}}` frame
(~118KB for a short thread). The fields that matter in `conversationState`:

| Field | Use |
| --- | --- |
| `threadRuntimeStatus` | `{"type": "active"\|"idle", "activeFlags": []}` — busy/idle |
| `requests` | pending approval/input requests, each `{id, method}` |
| `turnHistory.history.entitiesByKey` | turns, keyed `tail:N:local:<uuid>` |
| `currentPermissions` | `approvalPolicy`, `approvalsReviewer`, `sandboxPolicy.writableRoots` |
| `cwd`, `rolloutPath`, `id` | thread identity and location |

## The single-writer lock

Codex's thread store allows one writer per thread. The Desktop app holds a writer
lock on every thread it has open — **running or idle** — as an open fd on
`$CODEX_HOME/thread-writer-locks/<thread-id>.lock`. On this machine that was 58
of 377 indexed threads.

Consequences, all verified:

- `codex exec resume <id>` on a thread the app owns fails with
  `thread-store conflict: thread <id> already has an active writer` (code -32600).
- With no lock, `codex exec resume` works and continues the thread in place under
  the same session id, restoring full history from disk.
- Archiving in the app releases the lock and moves the rollout to
  `archived_sessions/`; `codex unarchive <id>` then makes it resumable.
- `codex mcp-server`'s `codex-reply` cannot resume *any* thread it did not create
  in its own process — it is not a route to existing threads.

**Never delete a lock file to get around this.** A second process would flock a
fresh inode and both would write one rollout. The IPC path is the sanctioned way
to write to a thread the app owns — that is the whole point of it.

## Thread settings — verified

`thread-follower-update-thread-settings` (v1) takes
`{conversationId, threadSettings}` and applies to the *next* turn
(`updateThreadSettingsForNextTurn`), so a running turn keeps what it started
with. The settable fields, per the app-server's `ThreadSettingsUpdateParams`:

`cwd`, `model`, `effort`, `summary`, `personality`, `serviceTier`,
`collaborationMode`, `multiAgentMode`, `approvalPolicy`, `approvalsReviewer`,
`sandboxPolicy`, `permissions`.

Verified live in one call: `effort` low→high, `collaborationMode` default→**plan**,
`serviceTier` default→**priority**.

- **Plan mode** is `collaborationMode`. It requires **both** halves —
  `{mode, settings}` with `settings.model` mandatory (`mode` is `plan` or
  `default`). Sending `{"mode": "plan"}` alone fails with
  `Invalid request: missing field 'settings'`, which reads as though the outer
  settings object were missing rather than this nested one.
- **Fast mode** is `serviceTier`: `default`, `flex`, `priority`, `scale`.
- `modelProvider` and `activePermissionProfile` appear in a snapshot's
  `latestThreadSettings` but are **not** accepted as update params.

## Goals and slash commands — verified

`thread/goal/set|clear|get` are app-server methods with no `thread-follower-*`
wrapper, so the follower protocol cannot set a goal directly. But the renderer
parses `/goal` out of ordinary message text (`/^\/goal(?=$| )/`), and that
parsing happens on the received input — so **sending `/goal <objective>` through
`thread-follower-start-turn` does set a real goal**. Verified: the objective
appeared in `completedThreadGoal` with `tokensUsed` and `timeUsedSeconds`, and
the work was actually carried out.

`/goal` and `/new` are the only composer slash commands. `/plan` is not one —
plan mode is the `collaborationMode` setting above. `/compact` is not needed
either: `thread-follower-compact-thread` (v1) is a direct method.

## Divergences

remodex's payload construction
(`phodex-bridge/src/desktop-ipc-action-follower.js:1864-1897`) disagrees with the
installed renderer:

| | remodex sends | installed app reads |
| --- | --- | --- |
| interrupt | `{conversationId, turnId}` | `params.mode`, `params.expectedTurnId` (never reads `turnId`) |
| start-turn | `{conversationId, senderRequestId, turnStartParams}` | `params.turnStart` |
| steer | `{conversationId, input, expectedTurnId}` | positional `(input, restoreMessage, serviceTier, attachments, clientUserMessageId, additionalContext)` |

Where they disagree, the installed bundle wins.

## Queued follow-ups — decoded

The app's build exposes a full queue API (`thread/queue/add | delete | list |
reorder | start | update`, plus `thread/revert`) that the older CLI build does
not have at all. Those are app-server methods; the only follower wrapper is
`thread-follower-set-queued-follow-ups-state` (v1), and it is a **whole-queue
replace**, not an append:

```js
let {conversationId: a, state: o} = t.params;
let s = o[a] ?? [];        // state is keyed by conversationId
await vet(r, a, s);        // persists exactly this list
```

So adding one follow-up means reading the current queue and sending the whole
list back. `thread-queued-followups-changed` (v1) is broadcast on success.

`thread/revert` takes `{threadId, beforeTurnId}`.

## Drift

A Codex Desktop update can bump any pinned version. The symptom is
`no-client-found` on a thread the app visibly owns — the same error as "nobody
owns this thread". If owner discovery succeeds but a follower request fails that
way, suspect version drift.

    uv run python scripts/extract_registry.py           # diff every installed bundle
    uv run python scripts/extract_registry.py --check   # exit 1 on drift

`tests/test_registry_drift.py` runs the same check in the suite, per bundle —
Doppel clones ship a patched `app.asar`, so a clone can drift while the stock app
does not. Verified today: all three installed bundles (stock, Personal, Veridue)
carry identical 22-method maps, so Doppel's patching does not touch the
protocol.
