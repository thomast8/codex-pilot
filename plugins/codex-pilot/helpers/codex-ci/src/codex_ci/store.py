"""Durable bindings and delivery ledger for the local CI helper."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .events import Event


@dataclass(frozen=True)
class Batch:
    ids: tuple[int, ...]
    events: tuple[Event, ...]
    instance: str
    thread: str


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    repo TEXT NOT NULL, pr INTEGER NOT NULL,
                    instance TEXT NOT NULL, thread TEXT NOT NULL,
                    PRIMARY KEY (repo, pr)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    repo TEXT NOT NULL, pr INTEGER NOT NULL, sha TEXT NOT NULL,
                    kind TEXT NOT NULL, url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', error TEXT
                );
                CREATE INDEX IF NOT EXISTS pending_events ON events(status, repo, pr, sha);
                """
            )
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def bind(self, repo: str, pr: int, instance: str, thread: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO bindings VALUES (?, ?, ?, ?) "
                "ON CONFLICT(repo, pr) DO UPDATE SET "
                "instance=excluded.instance, thread=excluded.thread",
                (repo, pr, instance, thread),
            )

    def bindings(self) -> list[dict[str, str | int]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM bindings ORDER BY repo, pr")]

    def enqueue(self, events: list[Event]) -> int:
        inserted = 0
        with self._connect() as db:
            for event in events:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO events(event_key,repo,pr,sha,kind,url) "
                    "VALUES (?,?,?,?,?,?)",
                    (event.key, event.repo, event.pr, event.sha, event.kind, event.url),
                )
                inserted += cursor.rowcount
        return inserted

    def claim(self) -> Batch | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute(
                "SELECT e.repo,e.pr,e.sha,b.instance,b.thread FROM events e "
                "JOIN bindings b USING (repo,pr) WHERE e.status='pending' "
                "ORDER BY e.id LIMIT 1"
            ).fetchone()
            if first is None:
                return None
            rows = db.execute(
                "SELECT * FROM events WHERE status='pending' AND repo=? AND pr=? AND sha=? "
                "ORDER BY id",
                (first["repo"], first["pr"], first["sha"]),
            ).fetchall()
            ids = tuple(row["id"] for row in rows)
            db.executemany(
                "UPDATE events SET status='inflight' WHERE id=?", [(id_,) for id_ in ids]
            )
            events = tuple(
                Event(row["event_key"], row["repo"], row["pr"], row["sha"], row["kind"], row["url"])
                for row in rows
            )
            return Batch(ids, events, first["instance"], first["thread"])

    def finish(self, batch: Batch, status: str, error: str | None = None) -> None:
        if status not in {"delivered", "held"}:
            raise ValueError("invalid delivery status")
        with self._connect() as db:
            db.executemany(
                "UPDATE events SET status=?, error=? WHERE id=?",
                [(status, error, id_) for id_ in batch.ids],
            )

    def retry(self, repo: str, pr: int) -> int:
        with self._connect() as db:
            result = db.execute(
                "UPDATE events SET status='pending',error=NULL "
                "WHERE repo=? AND pr=? AND status IN ('held','inflight')",
                (repo, pr),
            )
            return result.rowcount

    def counts(self) -> list[dict[str, str | int]]:
        with self._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT repo,pr,status,COUNT(*) AS count FROM events "
                    "GROUP BY repo,pr,status ORDER BY repo,pr,status"
                )
            ]
