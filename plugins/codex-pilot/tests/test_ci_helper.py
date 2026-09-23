"""Security and delivery behavior at the new helper's boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from codex_ci.delivery import deliver_one
from codex_ci.events import Event, WebhookError, parse_event, verify_signature
from codex_ci.server import handler_for
from codex_ci.store import Store

SHA = "a" * 40


def check_payload(conclusion: str = "failure") -> bytes:
    return json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "owner/repo"},
            "check_run": {
                "id": 42,
                "conclusion": conclusion,
                "head_sha": SHA,
                "html_url": "https://github.com/owner/repo/runs/42",
                "pull_requests": [{"number": 7}],
            },
        }
    ).encode()


def test_webhook_needs_a_valid_signature_and_ignores_success() -> None:
    body = check_payload()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    verify_signature(body, signature, "secret")
    with pytest.raises(WebhookError):
        verify_signature(body + b" ", signature, "secret")
    assert parse_event("check_run", check_payload("success")) == []
    assert parse_event("check_run", body) == [
        Event(
            "check_run:42:7", "owner/repo", 7, SHA,
            "check_run failure", "https://github.com/owner/repo/runs/42",
        )
    ]


def test_check_suite_without_html_url_and_own_review_are_handled() -> None:
    suite = json.loads(check_payload())
    suite["check_suite"] = suite.pop("check_run")
    suite["check_suite"].pop("html_url")
    events = parse_event("check_suite", json.dumps(suite).encode())
    assert events[0].url == "https://github.com/owner/repo/pull/7/checks"

    review = {
        "action": "submitted",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "author", "type": "User"},
        "pull_request": {"number": 7, "head": {"sha": SHA}},
        "pull_request_review": {
            "id": 8, "state": "changes_requested",
            "html_url": "https://github.com/owner/repo/pull/7#review-8",
        },
    }
    assert parse_event(
        "pull_request_review", json.dumps(review).encode(), frozenset({"author"})
    ) == []


def test_pr_conversation_comment_is_delivered_without_a_head_sha() -> None:
    comment = {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "reviewer", "type": "User"},
        "issue": {"number": 7, "pull_request": {"url": "https://api.github.com/pulls/7"}},
        "comment": {"id": 9, "html_url": "https://github.com/owner/repo/pull/7#issuecomment-9"},
    }
    events = parse_event("issue_comment", json.dumps(comment).encode())
    assert len(events) == 1
    assert events[0].sha == ""
    assert events[0].kind == "PR conversation comment"
    comment["issue"].pop("pull_request")
    assert parse_event("issue_comment", json.dumps(comment).encode()) == []


def test_review_comment_prompts_for_verified_reply_and_exact_resolution(tmp_path: Path) -> None:
    payload = {
        "action": "created",
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "reviewer", "type": "User"},
        "pull_request": {"number": 7, "head": {"sha": SHA}},
        "comment": {
            "id": 9,
            "html_url": "https://github.com/owner/repo/pull/7#discussion_r9",
        },
    }
    store = Store(tmp_path / "events.sqlite3")
    store.bind("owner/repo", 7, "personal", "thread-id")
    events = parse_event("pull_request_review_comment", json.dumps(payload).encode())
    assert store.enqueue(events) == 1

    class Session:
        def send_message(self, ref: str, text: str, instance: str | None = None) -> dict[str, Any]:
            assert ref == "thread-id" and instance == "personal"
            assert "#discussion_r9" in text
            assert "make and verify an actionable fix" in text
            assert "resolve only the exact fixed review thread" in text
            assert "leave that thread unresolved" in text
            return {"route": "desktop"}

        def focus_thread(self, ref: str, instance: str | None = None) -> dict[str, Any]:
            raise AssertionError("focus should not be needed")

    assert deliver_one(store, Session()) == {
        "status": "delivered", "repo": "owner/repo", "pr": 7, "events": 1,
        "instance": "personal", "thread": "thread-id", "route": "desktop",
    }


def test_delivery_uses_explicit_binding_and_deduplicates(tmp_path: Path) -> None:
    store = Store(tmp_path / "events.sqlite3")
    events = parse_event("check_run", check_payload())
    assert store.enqueue(events) == 1
    assert store.enqueue(events) == 0
    assert store.claim() is None  # no implicit task lookup
    store.bind("owner/repo", 7, "personal", "thread-id")

    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

        def send_message(
            self, ref: str, text: str, instance: str | None = None
        ) -> dict[str, Any]:
            self.calls.append((ref, text, instance))
            return {"route": "desktop"}

        def focus_thread(self, ref: str, instance: str | None = None) -> dict[str, Any]:
            raise AssertionError("focus should not be needed")

    session = Session()
    result = deliver_one(store, session)
    assert result is not None and result["status"] == "delivered"
    assert len(session.calls) == 1
    assert session.calls[0][0] == "thread-id"
    assert session.calls[0][2] == "personal"
    assert "Do not merge" in session.calls[0][1]
    assert deliver_one(store, session) is None


def test_interrupted_delivery_requires_explicit_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "events.sqlite3")
    store.bind("owner/repo", 7, "personal", "thread-id")
    store.enqueue(parse_event("check_run", check_payload()))
    batch = store.claim()
    assert batch is not None
    assert store.claim() is None
    assert store.retry("owner/repo", 7) == 1


def test_signed_http_ingress_is_durable_and_deduplicated(tmp_path: Path) -> None:
    store = Store(tmp_path / "events.sqlite3")
    wake = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store, "secret", wake))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        body = check_payload()
        good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

        def post(signature: str) -> dict[str, int]:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/github", body,
                headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "check_run"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.load(response)

        with pytest.raises(urllib.error.HTTPError) as error:
            post("sha256=" + "0" * 64)
        assert error.value.code == 400
        assert post(good)["accepted"] == 1
        assert post(good)["accepted"] == 0
        assert wake.is_set()
        assert store.counts() == [
            {"repo": "owner/repo", "pr": 7, "status": "pending", "count": 1}
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
