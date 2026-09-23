# Codex CI helper

A local GitHub webhook receiver that sends review comments and CI failures into
an explicitly bound Codex Desktop task. It installs `codex-desktop-core` and
does not install the Claude Code `codex-pilot` frontend.

From this directory:

```sh
uv run --no-editable codex-ci bind OWNER/REPO 123 --instance default --thread TASK_UUID
CODEX_CI_WEBHOOK_SECRET=... uv run --no-editable codex-ci serve --ignore-user YOUR_GITHUB_LOGIN
uv run --no-editable codex-ci status
```

The endpoint is `POST http://127.0.0.1:8765/github` by default. Subscribe to
`pull_request_review_comment`, `pull_request_review`, and `issue_comment` for
review replies, plus `check_run`, `check_suite`, and `workflow_run` for CI.
Configure a trusted HTTPS forwarder to this endpoint. The forwarder needs a
durable queue and retry if events must survive this Mac being offline. The
helper verifies `X-Hub-Signature-256`, persists received events in SQLite,
deduplicates them, and sends only links and identifiers into Codex. It never
guesses a target task or uses webhook comment text as instructions.

The task is asked to inspect the current comment, make and verify a fix, then
reply to and resolve only that exact review thread. Unverified or disputed
comments stay unresolved. The helper never merges a PR.

Some check events have no PR association; they are ignored. A delivery whose
outcome is uncertain is held for inspection. `codex-ci retry OWNER/REPO 123`
resends held events only when you explicitly request it, and can duplicate a
message if the first send actually landed.
