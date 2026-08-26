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

**A locked thread is not always a reachable one.** The app holds a writer lock
on every thread it has open, but only answers for the one a window is actually
showing. A thread it is holding in the background can be driven by neither route
until something surfaces it -- `focus_thread` does that.

**Approvals only reach you when the thread asks a human.** With
`approvalsReviewer` set to `auto_review` (the default) a subagent decides
escalations on its own and nothing ever appears in `thread_status`. Set it to
`user` via `edit_thread` on threads you intend to supervise.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import snapshot
from .actions import ActionError, Session
from .ipc import IpcError
from .threads import ThreadError

server = MCPServer(
    name="codex-pilot",
    version="0.1.0",
    instructions=__doc__,
)

_session: Session | None = None


def session() -> Session:
    global _session
    if _session is None:
        _session = Session()
    return _session


def _fail(exc: Exception) -> dict[str, Any]:
    """Errors are returned, not raised, so Claude can read and act on them."""
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


@server.tool(
    description=(
        "List Codex Desktop threads across every installed instance. Shows which "
        "instance each belongs to, whether the app currently owns it (route "
        "'desktop') or it is free to resume detached (route 'detached'), its "
        "working directory, and how recently it did anything."
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
        "waiting on an answer for. Read this before steering, stopping or "
        "responding. A null state means the read failed -- it does NOT mean "
        "nothing is pending."
    )
)
def thread_status(thread: str, instance: str | None = None) -> dict[str, Any]:
    try:
        sess = session()
        resolved = sess.resolve(thread, instance)
        out: dict[str, Any] = {
            "ok": True,
            "instance": resolved.instance.slug,
            "thread": resolved.thread_id,
            "name": resolved.name,
            "route": "desktop" if resolved.info.app_owned else "detached",
            "cwd": resolved.info.cwd,
            "archived": resolved.info.archived,
        }
        if not resolved.info.app_owned:
            out["state"] = None
            out["note"] = "not open in the app; no live state to read"
            return out
        state = snapshot.project(sess.snapshot(resolved))
        if state is None:
            out["state"] = None
            out["note"] = "could not read stream state -- check the app UI"
            return out
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
            "pending": [
                {
                    "request_id": p.request_id,
                    "kind": p.kind,
                    "summary": p.summary,
                    "reason": p.reason,
                    "cwd": p.cwd,
                    "available_decisions": p.available_decisions,
                    "answerable": p.answerable,
                }
                for p in state.pending
            ],
        }
        return out
    except (ActionError, IpcError, ThreadError) as exc:
        return _fail(exc)


@server.tool(
    description=(
        "Start a new turn on a thread. Routes itself: over IPC when Codex Desktop "
        "has the thread open, otherwise by resuming it detached (unarchiving it "
        "first if needed) and returning a pid and log path. A detached run "
        "returns immediately -- poll the log, it is not streamed. Use steer_turn "
        "instead when a turn is already running."
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
        "Interrupt the running turn. Pass expected_turn_id (from thread_status) to "
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
        "Bring a thread forward in Codex Desktop so a window claims it. Use this "
        "when a tool reports that a thread holds a writer lock but no window "
        "claims it: the app keeps threads open in the background without "
        "rendering them, and only answers for the one it is showing. Wait a "
        "couple of seconds, then retry the call that failed."
    )
)
def focus_thread(thread: str, instance: str | None = None) -> dict[str, Any]:
    try:
        return {"ok": True, **session().focus_thread(thread, instance=instance)}
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
