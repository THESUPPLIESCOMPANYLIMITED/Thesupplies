"""Safe webhook intake scaffold for the Supply automation platform.

This service authenticates and deduplicates events; it deliberately does not
execute downstream business actions. Run it behind a TLS-terminating reverse
proxy and connect an approved durable queue/worker before production use.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_BODY_BYTES = 1_048_576
MAX_CLOCK_SKEW_SECONDS = 300
SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def signature_for(secret: str | bytes, timestamp: str, event_id: str, body: bytes) -> str:
    """Return the required `sha256=<hex>` signature for a request."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    signed = timestamp.encode("ascii") + b"." + event_id.encode("ascii") + b"." + body
    return "sha256=" + hmac.new(key, signed, hashlib.sha256).hexdigest()


def signature_is_valid(
    secret: str | bytes,
    timestamp: str,
    event_id: str,
    body: bytes,
    supplied: str,
    *,
    now: float | None = None,
    tolerance: int = MAX_CLOCK_SKEW_SECONDS,
) -> bool:
    """Validate freshness and an HMAC-SHA256 signature in constant time."""
    try:
        sent_at = int(timestamp)
        event_id.encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance:
        return False
    expected = signature_for(secret, timestamp, event_id, body)
    return hmac.compare_digest(expected, supplied)


def payload_digest(body: bytes) -> str:
    """Return a non-reversible SHA-256 digest; callers must not persist body."""
    return hashlib.sha256(body).hexdigest()


def initialize_store(db_path: str | Path) -> None:
    """Create the receipt database and schema before serving requests."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS webhook_receipts (
                source TEXT NOT NULL,
                event_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                received_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('accepted')),
                PRIMARY KEY (source, event_id)
            )"""
        )


def record_event(
    db_path: str | Path,
    source: str,
    event_id: str,
    digest: str,
    received_at: str | None = None,
) -> str:
    """Persist an event receipt without storing the event body.

    Returns `created`, `duplicate`, or `conflict` (same ID, different body).
    """
    path = Path(db_path)
    initialize_store(path)
    stamp = received_at or datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout = 10000")
        cur = conn.execute(
            "INSERT OR IGNORE INTO webhook_receipts "
            "(source, event_id, payload_sha256, received_at, status) VALUES (?, ?, ?, ?, 'accepted')",
            (source, event_id, digest, stamp),
        )
        row = conn.execute(
            "SELECT payload_sha256 FROM webhook_receipts WHERE source = ? AND event_id = ?",
            (source, event_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Receipt persistence failed")
        if not hmac.compare_digest(row[0], digest):
            return "conflict"
        return "created" if cur.rowcount == 1 else "duplicate"


def secret_env_name(source: str) -> str:
    return "WEBHOOK_SECRET_" + re.sub(r"[^A-Z0-9]", "_", source.upper())


def json_response(handler: BaseHTTPRequestHandler, status: int, data: dict[str, Any]) -> None:
    encoded = json.dumps(data, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(encoded)


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "SupplyAutomationIntake/0.1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        # Avoid writing request metadata (including client addresses) by default.
        return

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path == "/healthz":
            json_response(self, 200, {"status": "ok"})
            return
        json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/v1/events/([a-z0-9][a-z0-9_-]{0,63})", path)
        if not match:
            json_response(self, 404, {"error": "not_found"})
            return
        source = match.group(1)
        if not SOURCE_RE.fullmatch(source):
            json_response(self, 400, {"error": "invalid_source"})
            return

        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            status = 413 if length > MAX_BODY_BYTES else 400
            json_response(self, status, {"error": "invalid_content_length"})
            return
        body = self.rfile.read(length)
        if len(body) != length:
            json_response(self, 400, {"error": "incomplete_body"})
            return
        try:
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("JSON object required")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            json_response(self, 400, {"error": "invalid_json_object"})
            return

        event_id = self.headers.get("X-Event-Id", "")
        timestamp = self.headers.get("X-Event-Timestamp", "")
        signature = self.headers.get("X-Webhook-Signature", "")
        if not EVENT_ID_RE.fullmatch(event_id):
            json_response(self, 400, {"error": "invalid_event_id"})
            return
        secret = os.environ.get(secret_env_name(source))
        if not secret:
            json_response(self, 503, {"error": "source_not_configured"})
            return
        if not signature_is_valid(secret, timestamp, event_id, body, signature):
            json_response(self, 401, {"error": "invalid_signature_or_timestamp"})
            return

        try:
            result = record_event(self.server.db_path, source, event_id, payload_digest(body))  # type: ignore[attr-defined]
        except (OSError, sqlite3.Error, RuntimeError):
            json_response(self, 503, {"error": "receipt_store_unavailable"})
            return
        if result == "conflict":
            json_response(self, 409, {"error": "event_id_payload_conflict"})
            return
        json_response(self, 202, {"accepted": True, "duplicate": result == "duplicate"})


class IntakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], db_path: str | Path):
        initialize_store(db_path)
        super().__init__(address, WebhookHandler)
        self.db_path = str(db_path)


def main() -> None:
    host = os.environ.get("AUTOMATION_HOST", "127.0.0.1")
    port = int(os.environ.get("AUTOMATION_PORT", "8080"))
    db_path = os.environ.get("AUTOMATION_DB_PATH", "./data/receipts.sqlite3")
    server = IntakeServer((host, port), db_path)
    print(f"Webhook intake listening on {host}:{port}; downstream actions are disabled")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
