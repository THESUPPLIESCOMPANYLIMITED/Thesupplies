import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from supply_automation.server import (
    IntakeServer,
    record_event,
    signature_for,
    signature_is_valid,
)


class WebhookSecurityTests(unittest.TestCase):
    def test_signature_verifies_and_binds_body_and_event_id(self):
        secret, timestamp, event_id, body = "test-secret", "1700000000", "evt-1", b'{"ok":true}'
        signature = signature_for(secret, timestamp, event_id, body)
        self.assertTrue(signature_is_valid(secret, timestamp, event_id, body, signature, now=1700000000))
        self.assertFalse(signature_is_valid(secret, timestamp, "evt-2", body, signature, now=1700000000))
        self.assertFalse(signature_is_valid(secret, timestamp, event_id, body + b" ", signature, now=1700000000))

    def test_rejects_stale_or_malformed_timestamps(self):
        signature = signature_for("s", "1699990000", "evt", b"{}")
        self.assertFalse(signature_is_valid("s", "1699990000", "evt", b"{}", signature, now=1700000000))
        self.assertFalse(signature_is_valid("s", "not-an-int", "evt", b"{}", signature, now=1700000000))

    def test_idempotent_receipts_and_conflicting_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "receipts.sqlite3"
            digest = hashlib.sha256(b'{"safe":true}').hexdigest()
            self.assertEqual(record_event(db, "crm", "evt-1", digest), "created")
            self.assertEqual(record_event(db, "crm", "evt-1", digest), "duplicate")
            other_digest = hashlib.sha256(b'{"safe":false}').hexdigest()
            self.assertEqual(record_event(db, "crm", "evt-1", other_digest), "conflict")
            with sqlite3.connect(db) as conn:
                rows = conn.execute("SELECT payload_sha256, status FROM webhook_receipts").fetchall()
            self.assertEqual(rows, [(digest, "accepted")])

    def test_database_does_not_store_raw_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "receipts.sqlite3"
            payload = b'{"email":"person@example.invalid"}'
            record_event(db, "crm", "evt-privacy", hashlib.sha256(payload).hexdigest())
            with sqlite3.connect(db) as conn:
                stored = conn.execute("SELECT payload_sha256 FROM webhook_receipts").fetchone()[0]
            self.assertNotIn("person@example.invalid", stored)
            self.assertEqual(stored, hashlib.sha256(payload).hexdigest())

    def test_http_health_signature_and_duplicate_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "receipts.sqlite3"
            server = IntakeServer(("127.0.0.1", 0), db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            secret, event_id = "integration-test-secret", "event-http-1"
            body = b'{"status":"created"}'
            timestamp = str(int(time.time()))
            signature = signature_for(secret, timestamp, event_id, body)
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-Event-Id": event_id,
                "X-Event-Timestamp": timestamp,
                "X-Webhook-Signature": signature,
            }
            try:
                with closing(http.client.HTTPConnection(host, port, timeout=3)) as client:
                    client.request("GET", "/healthz")
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), {"status": "ok"})
                with patch.dict(os.environ, {"WEBHOOK_SECRET_CRM": secret}):
                    for expected_duplicate in (False, True):
                        with closing(http.client.HTTPConnection(host, port, timeout=3)) as client:
                            client.request("POST", "/v1/events/crm", body=body, headers=headers)
                            response = client.getresponse()
                            data = json.loads(response.read())
                            self.assertEqual(response.status, 202)
                            self.assertEqual(data, {"accepted": True, "duplicate": expected_duplicate})
                    bad_headers = dict(headers, **{"X-Webhook-Signature": "sha256=" + "0" * 64})
                    with closing(http.client.HTTPConnection(host, port, timeout=3)) as client:
                        client.request("POST", "/v1/events/crm", body=body, headers=bad_headers)
                        response = client.getresponse()
                        self.assertEqual(response.status, 401)
                        self.assertEqual(json.loads(response.read())["error"], "invalid_signature_or_timestamp")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
