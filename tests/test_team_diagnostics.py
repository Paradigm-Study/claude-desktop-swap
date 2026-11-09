"""Failure diagnostics classify causes without retaining request or response data."""

from datetime import datetime
import errno
import http.client
import io
import json
import socket
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from team_pool import client
from team_pool import menu_bridge


SECRET = "synthetic-token-account-url-must-not-be-logged"


class StopLoop(Exception):
    pass


class TransportDiagnosticsTests(unittest.TestCase):
    def transport(self, *, response=None, error=None):
        transport = client.Transport("https://" + SECRET + ".invalid")
        stream = io.BytesIO(response or b"")
        transport.opener = Mock()
        if error is not None:
            transport.opener.open.side_effect = error
        else:
            transport.opener.open.return_value = stream
        return transport

    def failure(self, transport):
        with patch.object(client.time, "monotonic", side_effect=[10, 10.25]):
            with self.assertRaises(client.RequestError) as raised:
                transport.post("/v1/assignment", {"credential": SECRET}, token=SECRET)
        error = raised.exception
        self.assertEqual(error.endpoint, "/v1/assignment")
        self.assertEqual(error.duration_ms, 250)
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, json.dumps(error.__dict__))
        return error

    def test_network_causes_are_classified_without_exception_text(self):
        cases = [
            (TimeoutError(SECRET), "timeout"),
            (socket.gaierror(-2, SECRET), "dns"),
            (ssl.SSLCertVerificationError(1, SECRET), "tls"),
            (ConnectionRefusedError(SECRET), "connection"),
            (ConnectionResetError(SECRET), "connection"),
            (OSError(errno.ENETUNREACH, SECRET), "connection"),
            (http.client.IncompleteRead(SECRET.encode()), "connection"),
            (OSError(SECRET), "transport-error"),
            (ValueError(SECRET), "transport-error"),
        ]
        for cause, category in cases:
            for wrapped in (cause, urllib.error.URLError(cause)):
                with self.subTest(cause=type(cause).__name__, wrapped=wrapped is not cause):
                    error = self.failure(self.transport(error=wrapped))
                    self.assertEqual(error.category, category)
                    self.assertIsNone(error.status)
                    self.assertIsNone(error.coordinator_code)
        error = self.failure(self.transport(error=urllib.error.URLError(SECRET)))
        self.assertEqual(error.category, "transport-error")

    def test_invalid_json_shape_and_oversized_response_are_distinct(self):
        for raw, category in [
            (SECRET.encode(), "invalid-json"),
            (b"\xff", "invalid-json"),
            (json.dumps([SECRET]).encode(), "invalid-response"),
            (b"x" * (client.MAX_RESPONSE + 1), "response-too-large"),
        ]:
            with self.subTest(category=category):
                self.assertEqual(self.failure(self.transport(response=raw)).category, category)

    def test_only_known_coordinator_codes_survive_http_errors(self):
        for code in ("share-directory-unavailable", "no-eligible-account", SECRET, {"token": SECRET}):
            with self.subTest(known=isinstance(code, str) and code in client._COORDINATOR_CODES):
                body = io.BytesIO(json.dumps({"error": code, "message": SECRET,
                                             "access_token": SECRET}).encode())
                remote = urllib.error.HTTPError("https://invalid/" + SECRET, 503, SECRET,
                                                {"Authorization": SECRET}, body)
                error = self.failure(self.transport(error=remote))
                self.assertEqual(error.status, 503)
                self.assertEqual(error.category, "http-error")
                self.assertEqual(error.coordinator_code, code if isinstance(code, str)
                                 and code in client._COORDINATOR_CODES else None)
                self.assertTrue(body.closed)

    def test_error_body_is_bounded_and_always_closed(self):
        for payload in (b"x" * (client.MAX_RESPONSE + 10), SECRET.encode()):
            body = Mock()
            body.read.return_value = payload
            remote = urllib.error.HTTPError("https://invalid/" + SECRET, 502, SECRET, {}, body)
            error = self.failure(self.transport(error=remote))
            self.assertIsNone(error.coordinator_code)
            body.read.assert_called_once_with(client.MAX_RESPONSE + 1)
            body.close.assert_called_once()
        body = Mock()
        body.read.side_effect = TimeoutError(SECRET)
        remote = urllib.error.HTTPError("https://invalid/" + SECRET, 504, SECRET, {}, body)
        error = self.failure(self.transport(error=remote))
        self.assertEqual(error.status, 504)
        self.assertEqual(error.category, "http-error")
        body.close.assert_called_once()

    def test_valid_response_is_unchanged_and_success_is_silent(self):
        value = {"access_token": SECRET, "state": "fresh"}
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            result = self.transport(response=json.dumps(value).encode()).post("/v1/assignment", {})
        self.assertEqual(result, value)
        self.assertEqual(output.getvalue(), "")

    def test_unknown_endpoint_is_not_requested_or_echoed(self):
        transport = self.transport()
        with self.assertRaises(client.ClientError) as raised:
            transport.post("/v1/assignment?token=" + SECRET, {})
        transport.opener.open.assert_not_called()
        self.assertNotIn(SECRET, str(raised.exception))

    def test_request_error_constructor_is_compatible_and_sanitizes_metadata(self):
        error = client.RequestError(503)
        self.assertEqual(error.status, 503)
        self.assertEqual(error.category, "http-error")
        self.assertIsInstance(error, client.ClientError)
        self.assertEqual(client.RequestError().category, "transport-error")
        error = client.RequestError(SECRET, category=SECRET, endpoint=SECRET,
                                    coordinator_code=SECRET, duration_ms=SECRET)
        self.assertIsNone(error.status)
        self.assertIsNone(error.endpoint)
        self.assertIsNone(error.coordinator_code)
        self.assertIsNone(error.duration_ms)
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, json.dumps(error.__dict__))
        for duration in (float("nan"), float("inf"), -1, True):
            self.assertIsNone(client.RequestError(duration_ms=duration).duration_ms)


class DaemonDiagnosticsTests(unittest.TestCase):
    def run_cycles(self, outcomes):
        delays = []
        fake = SimpleNamespace(sync=Mock(side_effect=outcomes), status=Mock(return_value={
            "state": "degraded", "reason": SECRET, "account_email": SECRET,
        }))

        def sleep(delay):
            delays.append(delay)
            if len(delays) == len(outcomes):
                raise StopLoop

        with patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(StopLoop):
                client._run(fake, sleep=sleep, jitter=lambda low, high: 1.0)
        raw = output.getvalue()
        self.assertNotIn(SECRET, raw)
        records = [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
        for record in records:
            self.assertTrue(record["timestamp"].endswith("Z"))
            datetime.fromisoformat(record["timestamp"])
            self.assertGreaterEqual(record["duration_ms"], 0)
        return records, delays

    def test_logs_each_failure_once_and_one_recovery_with_actual_backoff(self):
        failure = client.RequestError(503, category="http-error", endpoint="/v1/assignment",
                                      coordinator_code="share-directory-unavailable", duration_ms=100)
        records, delays = self.run_cycles([
            failure, failure, {"state": "active", "next_sync_in": 30},
            {"state": "active", "next_sync_in": 30},
        ])
        self.assertEqual(delays, [5, 10, 30, 30])
        self.assertEqual([record["event"] for record in records],
                         ["sync-failed", "sync-failed", "sync-recovered"])
        self.assertEqual([record["failed_attempts"] for record in records], [1, 2, 2])
        for index, record in enumerate(records[:2]):
            self.assertEqual(record["retry_in_seconds"], delays[index])
            self.assertEqual(record["category"], "http-error")
            self.assertEqual(record["http_status"], 503)
            self.assertEqual(record["coordinator_code"], "share-directory-unavailable")
            self.assertEqual(record["endpoint"], "/v1/assignment")
            self.assertEqual(record["request_duration_ms"], 100)

    def test_nontransport_error_text_is_never_logged(self):
        records, delays = self.run_cycles([client.ClientError(SECRET), {"state": "active"}])
        self.assertEqual(records[0]["category"], "client-error")
        self.assertEqual(delays, [5, 30])

    def test_healthy_cycles_emit_no_diagnostic_records(self):
        records, delays = self.run_cycles([{"state": "active"}, {"state": "active"}])
        self.assertEqual(records, [])
        self.assertEqual(delays, [30, 30])

    def test_revocation_is_logged_without_retry(self):
        fake = SimpleNamespace(sync=Mock(side_effect=client.RequestError(401)),
                               status=Mock(return_value={"state": "revoked"}))
        sleep = Mock()
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(client._run(fake, sleep=sleep, jitter=lambda low, high: 1), 2)
        sleep.assert_not_called()
        record = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(record["event"], "sync-stopped")
        self.assertEqual(record["http_status"], 401)
        self.assertIsNone(record["retry_in_seconds"])


class MenuDiagnosticsTests(unittest.TestCase):
    def test_bridge_serializes_only_allowlisted_request_error_kind(self):
        for category, expected in (("dns", "dns"), (SECRET, "transport-error")):
            with self.subTest(category=expected):
                output = io.StringIO()
                error = client.RequestError(category=category, endpoint=SECRET,
                                            coordinator_code=SECRET)
                with patch.object(menu_bridge.sys, "platform", "darwin"), \
                        patch.object(menu_bridge, "dispatch", side_effect=error):
                    result = menu_bridge.main(input_stream=io.StringIO('{"action":"list"}'),
                                              output_stream=output, factory=Mock())
                self.assertEqual(result, 1)
                reply = json.loads(output.getvalue())
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error_kind"], expected)
                self.assertNotIn(SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()
