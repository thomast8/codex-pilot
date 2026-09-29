"""Validate and reduce GitHub webhooks to actionable PR events."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


class WebhookError(ValueError):
    """The request is not an authenticated GitHub webhook."""


@dataclass(frozen=True)
class Event:
    key: str
    repo: str
    pr: int
    sha: str
    kind: str
    url: str


def verify_signature(body: bytes, signature: str | None, secret: str) -> None:
    if not signature or not signature.startswith("sha256="):
        raise WebhookError("missing SHA-256 signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookError("invalid SHA-256 signature")


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pr_numbers(payload: dict[str, Any], kind: str) -> list[int]:
    if kind == "issue_comment":
        issue = _object(payload.get("issue"))
        number = issue.get("number")
        return [number] if type(number) is int and number > 0 and issue.get("pull_request") else []
    if kind in {"pull_request_review", "pull_request_review_comment"}:
        number = _object(payload.get("pull_request")).get("number")
        return [number] if type(number) is int and number > 0 else []
    source = _object(payload.get(kind))
    prs = source.get("pull_requests")
    numbers = (
        [item.get("number") for item in prs if isinstance(item, dict)]
        if isinstance(prs, list) else []
    )
    return sorted({number for number in numbers if type(number) is int and number > 0})


def parse_event(
    kind: str, body: bytes, ignored_users: frozenset[str] = frozenset()
) -> list[Event]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebhookError("invalid JSON") from exc
    if not isinstance(payload, dict):
        raise WebhookError("webhook body must be an object")
    if kind in {"pull_request_review", "pull_request_review_comment", "issue_comment"}:
        actor = _object(payload.get("sender"))
        if actor.get("type") == "Bot" or str(actor.get("login", "")).lower() in ignored_users:
            return []
    repo = _object(payload.get("repository")).get("full_name")
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise WebhookError("missing repository identity")
    action = payload.get("action")
    source = _object(payload.get(kind))
    if kind in {"check_run", "check_suite", "workflow_run"}:
        if action != "completed" or source.get("conclusion") in {
            None, "success", "skipped", "neutral"
        }:
            return []
        sha = source.get("head_sha")
        label = f"{kind} {source.get('conclusion')}"
    elif kind == "pull_request_review":
        if action != "submitted" or source.get("state") not in {"changes_requested", "commented"}:
            return []
        sha = _object(_object(payload.get("pull_request")).get("head")).get("sha")
        label = f"review {source.get('state')}"
    elif kind == "pull_request_review_comment":
        if action != "created":
            return []
        sha = _object(_object(payload.get("pull_request")).get("head")).get("sha")
        label = "review comment"
        source = _object(payload.get("comment"))
    elif kind == "issue_comment":
        if action != "created":
            return []
        sha = ""
        label = "PR conversation comment"
        source = _object(payload.get("comment"))
    else:
        return []
    identifier = source.get("id")
    raw_url = source.get("html_url")
    if type(identifier) is not int:
        raise WebhookError("event is missing an id")
    if kind != "check_suite" and (
        not isinstance(raw_url, str) or not raw_url.startswith("https://")
    ):
        raise WebhookError("event is missing a URL")
    if sha != "" and (
        not isinstance(sha, str) or len(sha) != 40 or any(
            c not in "0123456789abcdef" for c in sha.lower()
        )
    ):
        raise WebhookError("event is missing a commit SHA")
    events = []
    for pr in _pr_numbers(payload, kind):
        url = (
            f"https://github.com/{repo}/pull/{pr}/checks"
            if kind == "check_suite" else raw_url
        )
        if not isinstance(url, str):
            raise WebhookError("event is missing a URL")
        events.append(Event(f"{kind}:{identifier}:{pr}", repo, pr, sha.lower(), label, url))
    return events
