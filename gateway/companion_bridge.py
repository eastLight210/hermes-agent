"""Localhost clarification bridge for hermes-on-desk.

This module keeps the bridge deliberately small:

- POST clarification events to hermes-on-desk over localhost
- accept clarification replies back over a localhost-only HTTP endpoint
- correlate replies to waiting clarify requests via correlation_id

The bridge is optional. If the companion app is absent or unreachable, callers
should continue using their existing clarify UX.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib import error, request

logger = logging.getLogger(__name__)

_DEFAULT_EVENTS_URL = "http://127.0.0.1:8757/events"
_DEFAULT_REPLY_HOST = "127.0.0.1"
_DEFAULT_REPLY_PORT = 8758
_REPLY_PATH = "/replies/clarification"

_pending_lock = threading.Lock()
_pending_clarifications: Dict[str, queue.Queue] = {}

_server_lock = threading.Lock()
_reply_server: Optional[ThreadingHTTPServer] = None
_reply_thread: Optional[threading.Thread] = None
_reply_target: Optional[str] = None


def is_enabled() -> bool:
    """Return whether the localhost companion bridge is enabled."""
    value = os.getenv("HERMES_COMPANION_BRIDGE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def default_events_url() -> str:
    return os.getenv("HERMES_COMPANION_EVENTS_URL", _DEFAULT_EVENTS_URL).strip() or _DEFAULT_EVENTS_URL


def default_reply_host() -> str:
    return os.getenv("HERMES_COMPANION_REPLY_HOST", _DEFAULT_REPLY_HOST).strip() or _DEFAULT_REPLY_HOST


def default_reply_port() -> int:
    raw = os.getenv("HERMES_COMPANION_REPLY_PORT", str(_DEFAULT_REPLY_PORT)).strip()
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_REPLY_PORT
    if 0 <= port <= 65535:
        return port
    return _DEFAULT_REPLY_PORT


def build_reply_target(host: Optional[str] = None, port: Optional[int] = None) -> str:
    return f"http://{host or default_reply_host()}:{port if port is not None else default_reply_port()}{_REPLY_PATH}"


def _iso8601_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _ClarificationReplyHandler(BaseHTTPRequestHandler):
    server_version = "HermesCompanionBridge/1.0"

    def do_POST(self) -> None:  # noqa: N802 - stdlib interface
        if self.path != _REPLY_PATH:
            self.send_error(HTTPStatus.NOT_FOUND, "unknown path")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid content length")
            return

        try:
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid json body")
            return

        if resolve_pending_clarification(payload):
            self._write_json(HTTPStatus.ACCEPTED, {"ok": True})
            return

        self._write_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": "no pending clarification for correlation_id"},
        )

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        logger.debug("companion bridge http: " + fmt, *args)

    def _write_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def ensure_reply_server() -> Optional[str]:
    """Start the localhost reply receiver once and return its reply target."""
    global _reply_server, _reply_thread, _reply_target

    if not is_enabled():
        return None

    with _server_lock:
        if _reply_server is not None and _reply_target is not None:
            return _reply_target

        host = default_reply_host()
        preferred_port = default_reply_port()
        bind_attempts = [preferred_port]
        if preferred_port != 0:
            bind_attempts.append(0)

        last_error = None
        for port in bind_attempts:
            try:
                server = ThreadingHTTPServer((host, port), _ClarificationReplyHandler)
                break
            except OSError as exc:
                last_error = exc
        else:
            logger.warning("Companion bridge reply receiver failed to bind: %s", last_error)
            return None

        thread = threading.Thread(
            target=server.serve_forever,
            name="hermes-companion-bridge",
            daemon=True,
        )
        thread.start()

        _reply_server = server
        _reply_thread = thread
        _reply_target = build_reply_target(host=host, port=server.server_port)
        logger.info("Companion bridge reply receiver listening on %s", _reply_target)
        return _reply_target


def stop_reply_server() -> None:
    """Stop the reply receiver. Primarily used by tests."""
    global _reply_server, _reply_thread, _reply_target

    with _server_lock:
        server = _reply_server
        thread = _reply_thread
        _reply_server = None
        _reply_thread = None
        _reply_target = None

    with _pending_lock:
        _pending_clarifications.clear()

    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    if thread is not None:
        thread.join(timeout=1.0)


def register_pending_clarification(correlation_id: str, response_queue: queue.Queue) -> None:
    with _pending_lock:
        _pending_clarifications[correlation_id] = response_queue


def unregister_pending_clarification(correlation_id: str) -> None:
    with _pending_lock:
        _pending_clarifications.pop(correlation_id, None)


def resolve_pending_clarification(payload: dict) -> bool:
    correlation_id = str(payload.get("correlation_id") or "").strip()
    reply = str(payload.get("reply") or "").strip()
    if not correlation_id or not reply:
        return False

    with _pending_lock:
        response_queue = _pending_clarifications.pop(correlation_id, None)

    if response_queue is None:
        return False

    try:
        response_queue.put_nowait(reply)
    except queue.Full:
        logger.debug("Pending clarification queue already full for %s", correlation_id)
        return False
    return True


def send_clarification_event(
    question: str,
    choices: Optional[list[str]],
    correlation_id: str,
    *,
    source: str = "chat",
) -> bool:
    """Send a clarification event to hermes-on-desk.

    Returns True when the companion acknowledged the event with a 2xx status.
    """
    if not is_enabled():
        return False

    reply_target = ensure_reply_server()
    if not reply_target:
        return False

    payload = {
        "type": "clarification",
        "state": "waiting",
        "title": "Need input",
        "summary": question,
        "source": source,
        "correlation_id": correlation_id,
        "requires_input": True,
        "reply_target": reply_target,
        "timestamp": _iso8601_now(),
    }
    if choices:
        payload["choices"] = list(choices)

    req = request.Request(
        default_events_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=0.25) as response:
            return 200 <= getattr(response, "status", 0) < 300
    except (error.URLError, TimeoutError, OSError) as exc:
        logger.debug("Companion bridge event send failed: %s", exc)
        return False
