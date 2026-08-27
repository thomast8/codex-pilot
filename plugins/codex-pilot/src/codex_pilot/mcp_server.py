"""MCP server exposing Codex Desktop thread control to Claude Code.

One long-lived process holds one IPC connection per Codex instance, so the
handshake happens once rather than per call.

Two things every caller should understand, because they shape what is possible
rather than merely how it is spelled:

**The writer lock decides the route, not you.** Codex's thread store allows a
single writer. A thread open in the app can only be driven over IPC; a thread
nothing holds can only be driven by resuming it detached. `send_message` picks
for you. The lock is never worked around -- two writers on one rollout corrupt
it.

**A locked thread is not always a reachable one, and the holder is not always
the app.** The app holds a writer lock on every thread it has open, but only
answers for the one a window is actually showing. A thread it is holding in the
background can be driven by neither route until something surfaces it --
`focus_thread` does that. A lock held by a `codex exec` run instead, ours or
another process's, reads as route `detached_running`, and focusing *that* is
the one thing never to do: it asks the app to open a rollout somebody else is
writing. `holder` on `thread_status` and `list_threads` says which case it is,
and `started_here` says whether the run is ours to stop.

**Approvals only reach you when the thread asks a human.** With
`approvalsReviewer` set to `auto_review` (the default) a subagent decides
escalations on its own and nothing ever appears in `thread_status`. Set it to
`user` via `edit_thread` on threads you intend to supervise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import transcript
from .actions import ActionError, Session
from .ipc import IpcError
from .resume import DetachedError
from .threads import ThreadError

server = MCPServer(
    name="codex-pilot",
    version="0.2.0",
    instructions=__doc__,
)

# Waiting here holds the caller's whole agent turn: nothing else can be checked,
# nothing reacted to, no clean interrupt. Short gaps are worth absorbing, a long
# turn is not -- `codex-pilot watch` does that from a shell for free.
MAX_WAIT_SECONDS = 30.0


def _watch_prefix_for(project: Path) -> str:
    """How the caller invokes the watch CLI, given where this module lives.

    Deliberately not `shutil.which`: this process runs inside the plugin's own
    uv environment, where the console script is always on PATH -- and the shell
    the caller will actually run the command in is not. Asking our own PATH
    answers the wrong question and hands back "command not found".

    A pyproject beside us means we are running from the project rather than
    from an installed wheel, so the command needs `--project` to find it.
    """
    if (project / "pyproject.toml").is_file():
        return f"uv run --project {project} codex-pilot"
    return "codex-pilot"


def watch_prefix() -> str:
    return _watch_prefix_for(Path(__file__).resolve().parents[2])


def watch_command(thread: str = "<thread>", timeout: float = 900.0) -> str:
    return f"{watch_prefix()} watch {thread} --until turn_completed --timeout {timeout:g}"


_session: Session | None = None


def session() -> Session:
    global _session
    if _session is None:
        _session = Session()
    return _session


def _fail(exc: Exception) -> dict[str, Any]:
    """Errors are returned, not raised, so Claude can read and act on them."""
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


def _with_disk(
    out: dict[str, Any], rollout: Path | None, age_seconds: float | None
) -> dict[str, Any]:
    """Say what the rollout knows when the live stream told us nothing.

    `state: None` on its own is indistinguishable from "nothing pending", which
    is how a thread blocked on an approval goes unnoticed. `pending_known` is
    the machine-readable half of that: false means we could not look, never that
    we looked and found nothing.

    The disk block cannot supply the pending set -- rollouts hold no record of a
    request that has not been answered yet -- but it does separate a thread
    abandoned mid-turn from one that is simply idle, which is the distinction a
    supervisor actually has to act on.
    """
    out["pending_known"] = False
    try:
        phase = transcript.rollout_turn_phase(rollout) if rollout is not None else None
    except Exception:  # noqa: BLE001 - a bad rollout must not fail the whole call
        phase = None
    if phase is None:
        out["disk"] = None
        return out
    out["disk"] = {**phase.as_dict(), "age_seconds": age_seconds}
    return out


@server.tool(
    description=(
        "List Codex threads across every installed instance: which instance each "
        "belongs to, whether the app owns it (route 'desktop') or it is free to "
        "resume detached (route 'detached'), its working directory, how recently "
        "it did anything, and its `rollout` path -- read that file to see what the "
        "thread actually said. Threads started by start_thread stay listed after "
        "they go idle, marked `started_here`."
    )
)
def list_threads(instance: str | None = None) -> dict[str, Any]:
    try:
        threads = session().list_threads(instance)
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)
    return {
        "ok": True,
        "instances": [
            {"slug": i.slug, "codex_home": str(i.codex_home), "live": i.is_live}
            for i in session().instances
        ],
        "threads": threads,
    }


@server.tool(
    description=(
        "Inspect one thread: whether a turn is running, the current turn id, the "
        "model/reasoning/plan-mode settings in force, and anything the thread is "
        "waiting on an answer for, plus the `rollout` path to read its transcript. "
        "Read this before steering, stopping or responding. A null state means "
        "the read failed -- it does NOT mean nothing is pending. Branch on "
        "`pending_known`: false means the pending set could not be read at "
        "all. When it is false, `disk` reports what the rollout on disk "
        "still shows -- `phase: mid_turn` is a thread that was left inside a "
        "turn, which together with a large `age_seconds` is the signature of "
        "a wedged app: focus_thread it or check the UI."
    )
)
def thread_status(thread: str, instance: str | None = None) -> dict[str, Any]:
    try:
        sess = session()
        resolved = sess.resolve(thread, instance)
        rollout = resolved.info.rollout
        out: dict[str, Any] = {
            "ok": True,
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "name": resolved.name,
            "route": sess.route_for(resolved.info),
            # Who holds the writer lock, when anyone does. `started_here` says
            # whether that is one of our own runs; without the holder there is
            # nothing to tell a foreign writer from an app-held thread.
            "holder": str(resolved.info.holder) if resolved.info.holder else None,
            "lock_known": resolved.info.lock_known,
            "cwd": resolved.info.cwd,
            "archived": resolved.info.archived,
            "rollout": str(rollout) if rollout is not None else None,
        }
        out.update(sess.own_run_fields(resolved.thread_id))
        if sess.live_run(resolved.thread_id) is not None:
            # Our own CLI holds the lock, so the app has no live state for it.
            # Still false rather than absent: the field is the documented branch
            # point, and a caller should never have to guess a default for it.
            out["pending_known"] = False
            out["state"] = None
            out["note"] = (
                "a detached run started here is still going: read log_path, wait for "
                "turn_completed from collect_events, or stop_turn to terminate it"
            )
            return out
        holder = resolved.info.holder
        if holder is not None and holder.is_app is not True:
            # Locked, but not by the app. Saying only "not open in the app"
            # here reads as an idle thread waiting to be resumed, and it is the
            # opposite: something is writing it right now and neither route
            # reaches it. `pending_known` stays false because there is no live
            # state to have read the pending set from.
            out["pending_known"] = False
            out["state"] = None
            out["note"] = (
                f"the writer lock is held by {holder.described}; it is neither driveable "
                "over IPC nor free to resume until that process exits. Read the rollout "
                "(read_thread) for what it is doing, and do not focus_thread it."
            )
            return _with_disk(out, rollout, resolved.info.age_seconds)
        if not resolved.info.lock_known:
            out["pending_known"] = False
            out["state"] = None
            out["note"] = (
                "the writer lock could not be probed, so whether anything holds this "
                "thread is unknown -- it is not safe to resume on that basis. Check "
                "that `lsof` is usable."
            )
            return _with_disk(out, rollout, resolved.info.age_seconds)
        if not resolved.info.app_owned:
            out["state"] = None
            out["note"] = "not open in the app; no live state to read"
            return _with_disk(out, rollout, resolved.info.age_seconds)
        state = sess.thread_state(resolved)
        if state is None:
            out["state"] = None
            out["note"] = "could not read stream state -- check the app UI"
            return _with_disk(out, rollout, resolved.info.age_seconds)
        out["pending_known"] = True
        out["following"] = sess.follow_manager(resolved.instance).is_following(resolved.thread_id)
        out["state"] = {
            "runtime": state.runtime,
            "busy": state.busy,
            "turn_id": state.turn_id,
            "model": state.model,
            "effort": state.effort,
            "collaboration_mode": state.collaboration_mode,
            "service_tier": state.service_tier,
            "approvals_reviewer": state.approvals_reviewer,
            "goal": state.goal,
            "pending": [p.as_dict() for p in state.pending],
        }
        return out
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Start a new turn on an EXISTING thread. Returns as soon as the turn is "
        "accepted -- typically under a second, with the turn id and status "
        "'inProgress' -- and never waits for Codex to finish; use follow_thread "
        "plus collect_events to learn when it does. Routes itself: over IPC when "
        "Codex Desktop has the thread open, otherwise by resuming it detached "
        "(unarchiving first if needed) and returning a pid and log path. Use "
        "steer_turn instead when a turn is already running, and start_thread for "
        "work that needs a new thread."
    )
)
def send_message(
    thread: str,
    text: str,
    instance: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
) -> dict[str, Any]:
    try:
        result = session().send_message(
            thread, text, instance=instance, sandbox=sandbox, approval=approval
        )
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)
    return {"ok": True, **result}


@server.tool(
    description=(
        "Create a NEW Codex thread in a directory you name and start its first "
        "turn. Use this instead of running `codex exec` in a shell: it returns "
        "immediately with the new thread id -- it never waits for Codex to "
        "finish -- and the thread is then drivable by every other tool here "
        "(follow_thread, steer_turn, thread_status, focus_thread). "
        "Where it works is never guessed, so say which: either `cwd` for an "
        "existing directory, or `repo` plus `branch` to get a worktree made for "
        "it. The worktree goes where Codex puts its own, "
        "<CODEX_HOME>/worktrees/<id>/<repo> (override with "
        "CODEX_PILOT_WORKTREE_ROOT), on the new branch you name, off `base` if "
        "given. Prefer this for a slice of work that should not share a checkout "
        "with anything else. An existing branch is refused rather than reused, "
        "so an agent is never set loose on one that already carries other work. "
        "Note Codex clears old worktrees out of that root to save space, so "
        "commit and push the branch rather than parking work there. "
        "Defaults are consequential and worth setting deliberately: "
        "`sandbox` defaults to 'workspace-write' (the agent writes anywhere under "
        "cwd, no network) and may be 'read-only'; `approval` defaults to 'never' "
        "(nothing is asked of a human, because a detached run has no way to be "
        "asked) and may be 'untrusted', 'on-failure' or 'on-request' -- but those "
        "will stall a detached run, so use them only on a thread you intend to "
        "focus into the app. 'danger-full-access' is refused unless the server "
        "was started with CODEX_PILOT_ALLOW_FULL_ACCESS. "
        "While the run goes it holds the thread's writer lock, so route reads "
        "'detached_running' (with `started_here` true, since it is ours) and the "
        "thread cannot be steered or driven over IPC; "
        "stop_turn terminates it, and collect_events reports turn_completed (or "
        "run_failed) when it exits. Do NOT focus_thread it while it runs. Once "
        "it is idle, focus_thread brings it into Codex Desktop for IPC. Read "
        "`log_path` for streamed JSONL, or `rollout` for the transcript."
    )
)
def start_thread(
    text: str,
    cwd: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    base: str | None = None,
    instance: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    try:
        result = session().start_thread(
            text,
            cwd=cwd,
            repo=repo,
            branch=branch,
            base=base,
            instance=instance,
            sandbox=sandbox,
            approval=approval,
            model=model,
        )
    except (ActionError, IpcError, ThreadError, DetachedError) as exc:
        return _fail(exc)
    return {"ok": True, **result}


@server.tool(
    description=(
        "Inject text into a turn that is already running, without restarting it. "
        "This is the correction channel: the running turn sees the new input and "
        "adjusts. Requires the app to have the thread open."
    )
)
def steer_turn(thread: str, text: str, instance: str | None = None) -> dict[str, Any]:
    try:
        return {"ok": True, **session().steer_turn(thread, text, instance=instance)}
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Interrupt the running turn. On a thread one of our own detached runs is "
        "writing (route 'detached_running' with `started_here`) this terminates that "
        "run and its whole process group, and `stopped` says whether it was still "
        "alive. A detached writer this process did not start is refused rather than "
        "signalled -- its process group is not ours -- so stop it where it was started. "
        "Otherwise it interrupts over IPC: pass expected_turn_id (from thread_status) to "
        "refuse stopping a turn that started after you looked. Read `stopped` in "
        "the result, not `ok`: the app answers ok:true even when it stopped "
        "nothing, both for an already-idle thread and for a precondition that "
        "did not match."
    )
)
def stop_turn(
    thread: str, expected_turn_id: str | None = None, instance: str | None = None
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            **session().stop_turn(thread, expected_turn_id=expected_turn_id, instance=instance),
        }
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Answer something a thread is waiting on: a command or file-change "
        "approval, a permissions request, a tool's question, or an MCP "
        "elicitation. Take request_id, kind and available_decisions from "
        "thread_status and pass available_decisions through -- the valid answers "
        "differ per request, and one the app did not offer is ignored rather "
        "than refused. Note that acceptForSession and the two amendment "
        "decisions are standing grants that outlive the turn, unlike accept. "
        "Sent once, never retried: a timeout means the outcome is unknown, so "
        "check the app rather than resending."
    )
)
def respond(
    thread: str,
    request_id: int | str,
    kind: str,
    decision: Any = None,
    response: Any = None,
    available_decisions: list[Any] | None = None,
    instance: str | None = None,
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            **session().respond(
                thread,
                request_id,
                kind,
                decision=decision,
                response=response,
                instance=instance,
                available_decisions=available_decisions,
            ),
        }
    except (ActionError, IpcError, ThreadError, ValueError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Change a thread. action='update_settings' sets any of model, effort, "
        "personality, serviceTier ('priority' is fast mode), collaborationMode "
        "(plan mode -- build it with mode plus a settings object containing at "
        "least a model), multiAgentMode, approvalPolicy, approvalsReviewer "
        "('user' to make approvals visible to you), sandboxPolicy, permissions, "
        "cwd, summary; these apply to the NEXT turn, not a running one. "
        "action='compact' compacts the thread's context."
    )
)
def edit_thread(
    thread: str,
    action: str,
    settings: dict[str, Any] | None = None,
    instance: str | None = None,
) -> dict[str, Any]:
    try:
        sess = session()
        if action == "compact":
            return {"ok": True, **sess.compact(thread, instance=instance)}
        if action == "update_settings":
            if not settings:
                return {
                    "ok": False,
                    "error": "ValueError",
                    "message": "update_settings needs a `settings` object",
                }
            return {"ok": True, **sess.update_settings(thread, settings, instance=instance)}
        return {
            "ok": False,
            "error": "ValueError",
            "message": f"unknown action {action!r}; expected 'update_settings' or 'compact'",
        }
    except (ActionError, IpcError, ThreadError, ValueError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Start or stop following a thread. A follow keeps its state current in "
        "the background and records what changed, so collect_events can tell you "
        "when a turn finished or an approval appeared without you polling "
        "thread_status. Only works while the app has the thread mounted -- "
        "focus_thread first if a follow stays silent."
    )
)
def follow_thread(thread: str, follow: bool = True, instance: str | None = None) -> dict[str, Any]:
    try:
        return {"ok": True, **session().follow_thread(thread, follow=follow, instance=instance)}
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Drain events from followed threads: turn_started, turn_completed (a "
        "thread went idle and is free for work), request_pending, "
        "request_resolved, resync, follow_lost. A follow collects in the "
        "background whether or not you are polling, so passing the previous "
        "`cursor` with wait_seconds=0 returns everything that happened since "
        "you last looked, immediately and without waiting. Use a small "
        f"wait_seconds only to avoid a tight loop: it is capped at "
        f"{MAX_WAIT_SECONDS:g}s because a blocked tool call freezes this entire "
        "turn -- you cannot check another thread, react, or be interrupted "
        "while it runs -- and a `note` in the result says so whenever your wait "
        "was shortened. To wait out a turn that takes minutes, do not block "
        f"here. Background a shell watch instead: `{watch_command()}`, which "
        "costs no turn and reports when the thread goes idle. An empty result "
        "can also mean the app is holding the thread without rendering it, in "
        "which case the follow streams nothing until focus_thread. A non-zero "
        "`dropped` means the buffer overflowed and some events were lost."
        "\n\n`threads` reports each thread's health -- ok, resyncing, lost, "
        "detached or not_following -- with whatever it is waiting on. Read "
        "`pending` only together with `pending_known`: an empty list on a "
        "thread whose state cannot be read means 'no idea', NOT 'nothing "
        "pending'. A thread that reports not_following was never followed or "
        "was lost with the process, so nothing will ever arrive for it until "
        "you follow it again."
        "\n\nEvery response carries an `epoch`; pass it back alongside "
        "`cursor`. If the server restarted, sequence numbers began again and "
        "your cursor would silently discard everything after it -- a mismatch "
        "resets the cursor and sets `cursor_reset`. Omitting `epoch` leaves you "
        "relying on a best-effort check that only catches a cursor beyond "
        "any this process has issued, so it stops helping once the new "
        "process passes it."
    )
)
def collect_events(
    threads: list[str] | None = None,
    after: int = 0,
    wait_seconds: float = 0.0,
    instance: str | None = None,
    epoch: str | None = None,
) -> dict[str, Any]:
    asked = max(0.0, wait_seconds)
    granted = min(MAX_WAIT_SECONDS, asked)
    try:
        result: dict[str, Any] = {
            "ok": True,
            **session().collect_events(
                threads,
                after=after,
                wait_seconds=granted,
                instance=instance,
                epoch=epoch,
            ),
        }
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)
    if granted < asked:
        # Shortening the wait without saying so is indistinguishable from a
        # wait that ran its course and found nothing, which is how a caller
        # ends up believing a thread is still working hours after it stopped.
        # Name the thread the caller is actually watching where we can: a
        # placeholder they have to fill in is one more step between them and
        # the thing that works.
        candidates = threads or result.get("following") or []
        target = candidates[0] if len(candidates) == 1 else "<thread>"
        result["note"] = (
            f"wait_seconds capped at {granted:g}s (asked for {asked:g}s): a blocked "
            f"tool call freezes this whole turn. To wait longer without spending "
            f"one, run this in a background shell instead: "
            f"{watch_command(target, asked)}"
        )
    return result


@server.tool(
    description=(
        "Find out which threads Codex Desktop will actually answer for, and "
        "optionally bring the rest forward so they stream. Holding a writer lock "
        "and being reachable are different states: the app keeps threads open "
        "without rendering them, and an unrendered one sends no stream state at "
        "all, so a follow on it is silently empty. With mount=true this focuses "
        "the unmounted ones and re-checks. Mounting is additive, not a swap -- "
        "bringing one forward does not evict the others -- so treat this as a "
        "one-off warm-up for the threads you intend to watch, NOT something to "
        "cycle through repeatedly. Threads one of our own detached runs is "
        "writing are skipped rather than focused. Returns each thread with "
        "`mounted` and its owning client, plus `mounted_by_sync` for the ones "
        "this call gained."
    )
)
def sync_threads(
    threads: list[str] | None = None,
    mount: bool = True,
    instance: str | None = None,
) -> dict[str, Any]:
    try:
        return {"ok": True, **session().sync_threads(threads, mount=mount, instance=instance)}
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Read what a thread actually said, from its rollout on disk. This is the "
        "way to harvest a result, and it works for EVERY thread -- mounted or "
        "not, running or idle, app-owned or detached -- because it does not go "
        "through the app at all. Prefer it over re-running an agent to find out "
        "what happened. Returns the last `limit` entries newest-last: messages, "
        "tool calls and tool output. Reasoning traces are excluded unless you "
        "ask for them, as they dominate a rollout and are rarely what you want."
    )
)
def read_thread(
    thread: str,
    limit: int = 40,
    include_reasoning: bool = False,
    instance: str | None = None,
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            **session().read_thread(
                thread, limit=limit, include_reasoning=include_reasoning, instance=instance
            ),
        }
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Bring a thread forward in Codex Desktop so a window claims it. Use this "
        "when a tool reports that a thread holds a writer lock but no window "
        "claims it: the app keeps threads open in the background without "
        "rendering them, and only answers for the one it is showing. Wait a "
        "couple of seconds, then retry the call that failed.\n\n"
        "Only for a lock the app itself holds. On route 'detached_running' the "
        "holder is a `codex exec` run, and this is refused: asking the app to open "
        "a rollout another writer has is exactly the two-writer case the lock "
        "prevents. Wait for that run to exit, or stop_turn it if it is ours.\n\n"
        "Navigates in the background, so it does not steal focus from whatever "
        "the user is doing. Pass activate=true only if they asked to be shown "
        "the thread."
    )
)
def focus_thread(
    thread: str, instance: str | None = None, activate: bool = False
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            **session().focus_thread(thread, instance=instance, activate=activate),
        }
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Set a thread's goal -- a persistent objective it works toward across "
        "turns, with its own token and time budget. Delivered as a /goal message, "
        "which is how the app itself sets one."
    )
)
def set_goal(thread: str, objective: str, instance: str | None = None) -> dict[str, Any]:
    try:
        result = session().send_message(thread, f"/goal {objective}", instance=instance)
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)
    return {"ok": True, "objective": objective, **result}


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
