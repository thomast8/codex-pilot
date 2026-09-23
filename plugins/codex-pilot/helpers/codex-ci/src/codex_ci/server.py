"""Authenticated GitHub webhook receiver with a local durable queue."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from codex_desktop_core.actions import Session

from .delivery import deliver_one
from .events import WebhookError, parse_event, verify_signature
from .store import Store

MAX_BODY = 1024 * 1024


def handler_for(
    store: Store, secret: str, wake: threading.Event,
    ignored_users: frozenset[str] = frozenset(),
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/github":
                self.send_error(404)
                return
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal() or not 0 < int(length) <= MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(int(length))
            try:
                verify_signature(body, self.headers.get("X-Hub-Signature-256"), secret)
                events = parse_event(
                    self.headers.get("X-GitHub-Event", ""), body, ignored_users
                )
            except WebhookError as exc:
                self._json(400, {"error": str(exc)})
                return
            added = store.enqueue(events)
            if added:
                wake.set()
            self._json(202, {"accepted": added, "actionable": len(events)})

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            # Do not put authenticated event contents into the server log.
            return

    return Handler


def serve(
    store: Store, secret: str, host: str, port: int, debounce: float = 20,
    ignored_users: frozenset[str] = frozenset(),
) -> None:
    wake = threading.Event()
    stop = threading.Event()

    def worker() -> None:
        session = Session()
        try:
            while not stop.is_set():
                while (result := deliver_one(store, session)) is not None:
                    print(json.dumps(result), flush=True)
                wake.wait()
                wake.clear()
                if not stop.is_set():
                    stop.wait(debounce)
        except Exception as exc:
            print(
                json.dumps({"status": "helper_failed", "error": f"{type(exc).__name__}: {exc}"}),
                flush=True,
            )
            server.shutdown()
        finally:
            session.close()

    thread = threading.Thread(target=worker, name="codex-ci-delivery", daemon=True)
    server = ThreadingHTTPServer(
        (host, port), handler_for(store, secret, wake, ignored_users)
    )
    thread.start()
    try:
        print(json.dumps({"status": "listening", "host": host, "port": port}), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        print(json.dumps({"status": "stopped"}), flush=True)
    finally:
        stop.set()
        wake.set()
        server.server_close()
        thread.join(timeout=2)
