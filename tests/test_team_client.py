"""Client lifecycle tests use temporary state, fake Keychains and local HTTP only."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from team_pool import client
from team_pool.native import NativeDriftError, NativePolicyError


INVITE = "fixture-invitation-never-live"
DEVICE_TOKEN = "fixture-device-token-never-live"
PROVIDER_TOKEN = "fixture-provider-token-never-live"


def assignment(**changes):
    value = {
        "account_id": "account-a", "generation": 1, "access_token": PROVIDER_TOKEN,
        "expires_at": 4000, "scopes": ["user:inference", "user:profile"],
        "account": {"accountUuid": "test-account", "emailAddress": "example@example.invalid",
                    "organizationUuid": "test-organization", "subscriptionType": "max", "rateLimitTier": "default"},
        "renew_after": 30, "state": "fresh",
    }
    value.update(changes)
    return value


class FakeNative:
    def __init__(self, credentials=None):
        self.credentials = credentials
        self.original = None
        self.has_backup = False
        self.secrets = {}
        self.writes = []
        self.race = False
        self.partial_write = False

    def preflight(self):
        pass

    def snapshot(self):
        return {"has_credentials": self.credentials is not None,
                "fingerprint": hashlib.sha256(repr(self.credentials).encode()).hexdigest(),
                "has_backup": self.has_backup}

    def plan(self, value, expected, *, initialize=False):
        return {
            "before": self.snapshot()["fingerprint"],
            "keychain_only": hashlib.sha256(repr({"partial": value}).encode()).hexdigest(),
            "after": hashlib.sha256(repr(value).encode()).hexdigest(),
        }

    def get_secret(self, name):
        return self.secrets.get(name)

    def set_secret(self, name, value):
        self.secrets[name] = value

    def delete_secret(self, name):
        self.secrets.pop(name, None)

    def apply(self, value, expected, *, initialize=False):
        if self.race or expected != self.snapshot()["fingerprint"]:
            raise NativeDriftError("fixture drift")
        if initialize:
            self.original = copy.deepcopy(self.credentials)
            self.has_backup = True
        if self.partial_write:
            self.credentials = {"partial": copy.deepcopy(value)}
            raise SystemExit("simulated process death after first native write")
        self.credentials = copy.deepcopy(value)
        self.writes.append(copy.deepcopy(value))
        return self.snapshot()["fingerprint"]

    def restore(self, expected):
        if expected != self.snapshot()["fingerprint"]:
            raise NativeDriftError("fixture drift")
        self.credentials = self.original
        self.original = None
        self.has_backup = False
        return self.snapshot()["fingerprint"]


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.next = assignment()
        self.error = None

    def post(self, path, body, token=None):
        self.calls.append((path, body, token))
        if self.error:
            raise self.error
        if path == "/v1/enroll":
            return {"device_id": "device-a", "device_token": DEVICE_TOKEN}
        return copy.deepcopy(self.next)


class ClientLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state/team.json"
        self.native = FakeNative()
        self.transport = FakeTransport()
        self.now = 1000
        self.client = client.Client(self.path, native=self.native, transport=self.transport,
                                    clock=lambda: self.now, preflight=lambda: None)

    def enroll(self):
        return self.client.enroll("https://pool.example.invalid", "Test Mac", INVITE)

    def test_enrollment_then_rotation_keeps_secrets_out_of_files_and_status(self):
        self.enroll()
        self.assertIsNone(self.native.credentials)
        self.assertEqual(self.native.secrets[client.DEVICE_SECRET], DEVICE_TOKEN)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.client.sync()
        self.transport.next = assignment(account_id="account-b", generation=2, access_token="fixture-token-b")
        result = self.client.sync()
        self.assertEqual(result["account_id"], "account-b")
        self.assertEqual(len(self.native.writes), 2)
        for path in self.path.parent.iterdir():
            for secret in (INVITE, DEVICE_TOKEN, PROVIDER_TOKEN, "fixture-token-b"):
                self.assertNotIn(secret, path.read_text())
                self.assertNotIn(secret, json.dumps(self.client.status()))

    def test_existing_native_login_needs_explicit_first_takeover(self):
        self.native.credentials = {"personal": "fixture-personal"}
        self.enroll()
        with self.assertRaisesRegex(client.ClientError, "take-over-native-store"):
            self.client.sync()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.native.credentials, {"personal": "fixture-personal"})
        self.client.sync(take_over_native_store=True)
        self.client.leave(restore=True)
        self.assertEqual(self.native.credentials, {"personal": "fixture-personal"})
        self.assertFalse(self.path.exists())
        self.assertFalse(self.native.secrets)

    def test_network_failure_retains_last_credential_and_reports_expiry_truthfully(self):
        self.enroll()
        self.client.sync()
        original = copy.deepcopy(self.native.credentials)
        self.transport.error = client.RequestError(503)
        with self.assertRaises(client.RequestError):
            self.client.sync()
        self.assertEqual(self.native.credentials, original)
        self.assertEqual(self.client.status()["state"], "degraded")
        self.now = 5000
        self.assertEqual(self.client.status()["state"], "expired")
        self.assertTrue(self.client.status()["credential_expired"])

    def test_revocation_stops_future_calls_without_deleting_native_credentials(self):
        self.enroll()
        self.client.sync()
        self.transport.error = client.RequestError(401)
        with self.assertRaises(client.RequestError):
            self.client.sync()
        requests = len(self.transport.calls)
        with self.assertRaises(client.PausedError):
            self.client.sync(take_over_native_store=True)
        self.assertEqual(len(self.transport.calls), requests)
        self.assertEqual(self.client.status()["state"], "revoked")
        self.assertIsNotNone(self.native.credentials)

    def test_manual_logout_pauses_before_contacting_coordinator(self):
        self.enroll()
        self.client.sync()
        self.native.credentials = None
        requests = len(self.transport.calls)
        with self.assertRaises(client.PausedError):
            self.client.sync()
        self.assertEqual(len(self.transport.calls), requests)
        self.assertIsNone(self.native.credentials)
        self.assertEqual(self.client.status()["state"], "paused")

    def test_concurrent_login_between_snapshot_and_apply_is_preserved(self):
        self.enroll()
        self.client.sync()
        self.native.race = True
        with self.assertRaises(client.PausedError):
            self.client.sync()
        self.assertEqual(len(self.native.writes), 1)
        self.assertEqual(self.client.status()["state"], "paused")

    def test_repaired_policy_requires_explicit_resume_and_unchanged_native_login(self):
        self.enroll()
        self.client.sync()
        self.native.preflight = lambda: (_ for _ in ()).throw(NativePolicyError("fixture policy"))
        requests = len(self.transport.calls)
        with self.assertRaises(client.PausedError):
            self.client.sync()
        self.assertEqual(self.client.status()["state"], "paused")
        self.assertEqual(len(self.transport.calls), requests)
        self.native.preflight = lambda: None
        with self.assertRaises(client.PausedError):
            self.client.sync()
        self.client.resume()
        self.assertEqual(self.client.sync()["state"], "active")
        self.native.credentials = None
        with self.assertRaisesRegex(client.ClientError, "manual login or logout"):
            self.client.resume()

    def test_leave_without_restore_preserves_native_and_drift_refuses_restore(self):
        self.enroll()
        self.client.sync()
        self.native.credentials = {"manual": "fixture-different-login"}
        with self.assertRaisesRegex(client.ClientError, "restoration refused"):
            self.client.leave(restore=True)
        self.assertTrue(self.path.exists())
        self.assertIn(client.DEVICE_SECRET, self.native.secrets)
        self.client.leave()
        self.assertEqual(self.native.credentials, {"manual": "fixture-different-login"})
        self.assertTrue(self.path.exists())
        self.assertEqual(self.client.status()["state"], "left")
        self.assertFalse(self.native.secrets)

    def test_stale_and_exhausted_quota_surface_degraded_status(self):
        self.enroll()
        for state in ("quota_stale", "quota_exhausted"):
            self.transport.next = assignment(state=state)
            result = self.client.sync()
            self.assertEqual(result["state"], "degraded")
            self.assertEqual(result["reason"], state)

    def test_bad_or_expired_assignments_never_reach_native_store(self):
        self.enroll()
        for changes in ({"expires_at": 999}, {"expires_at": float("nan")},
                        {"renew_after": -1}, {"scopes": "user:inference"},
                        {"generation": True}, {"access_token": "bad\ntoken"}):
            self.transport.next = assignment(**changes)
            with self.subTest(changes=changes), self.assertRaises(client.ClientError):
                self.client.sync()
        self.assertEqual(self.native.writes, [])

    def test_different_enrollment_requires_explicit_leave(self):
        self.enroll()
        with self.assertRaisesRegex(client.ClientError, "recovery state already exists"):
            self.enroll()
        self.assertEqual(len(self.transport.calls), 1)

    def test_failed_first_sync_preserves_enrollment_for_retry(self):
        self.enroll()
        self.transport.error = client.RequestError()
        with self.assertRaises(client.RequestError):
            self.client.sync()
        self.assertTrue(self.path.exists())
        self.assertIn(client.DEVICE_SECRET, self.native.secrets)
        self.transport.error = None
        self.assertEqual(self.client.sync()["state"], "active")

    def restart(self):
        return client.Client(self.path, native=self.native, transport=self.transport,
                             clock=lambda: self.now, preflight=lambda: None)

    def test_completed_native_write_recovers_after_final_state_save_crash(self):
        self.enroll()
        self.client.sync()
        self.transport.next = assignment(account_id="account-b", generation=2, access_token="fixture-renewed")
        save = self.client._save

        def fail_final_save(state):
            if "pending_write" not in state:
                raise OSError("simulated crash saving completion")
            save(state)

        with patch.object(self.client, "_save", side_effect=fail_final_save):
            with self.assertRaises(OSError):
                self.client.sync()
        pending = json.loads(self.path.read_text())["pending_write"]
        self.assertEqual(pending["after"], self.native.snapshot()["fingerprint"])
        self.assertNotIn("fixture-renewed", self.path.read_text())
        self.assertNotIn(PROVIDER_TOKEN, self.path.read_text())
        recovered = self.restart()
        result = recovered.sync()
        self.assertEqual(result["account_id"], "account-b")
        self.assertEqual(result["state"], "active")
        self.assertNotIn("pending_write", json.loads(self.path.read_text()))

    def test_process_death_before_native_write_safely_retries_after_restart(self):
        self.enroll()
        with patch.object(self.native, "apply", side_effect=SystemExit("simulated death before write")):
            with self.assertRaises(SystemExit):
                self.client.sync()
        self.assertIsNone(self.native.credentials)
        self.assertIn("pending_write", json.loads(self.path.read_text()))
        result = self.restart().sync()
        self.assertEqual(result["state"], "active")
        self.assertEqual(len(self.native.writes), 1)

    def test_partial_native_write_pauses_and_explicit_restore_recovers_original(self):
        original = {"personal": "fixture-original-personal"}
        self.native.credentials = copy.deepcopy(original)
        self.enroll()
        self.native.partial_write = True
        with self.assertRaises(SystemExit):
            self.client.sync(take_over_native_store=True)
        requests = len(self.transport.calls)
        recovered = self.restart()
        with self.assertRaisesRegex(client.PausedError, "interrupted native write"):
            recovered.sync()
        self.assertEqual(len(self.transport.calls), requests)
        self.assertEqual(recovered.status()["reason"], "partial_native_write")
        result = recovered.leave(restore=True)
        self.assertTrue(result["native_restored"])
        self.assertEqual(self.native.credentials, original)
        self.assertFalse(self.path.exists())

    def test_pending_journal_never_authorizes_overwriting_unrelated_native_login(self):
        self.enroll()
        self.native.partial_write = True
        with self.assertRaises(SystemExit):
            self.client.sync()
        self.native.credentials = {"different": "fixture-unrelated-manual-login"}
        recovered = self.restart()
        with self.assertRaises(client.PausedError):
            recovered.sync()
        with self.assertRaisesRegex(client.ClientError, "restoration refused"):
            recovered.leave(restore=True)
        self.assertEqual(self.native.credentials, {"different": "fixture-unrelated-manual-login"})
        self.assertTrue(self.path.exists())

    def test_leave_preserves_recovery_until_later_explicit_restore(self):
        original = {"personal": "fixture-original-personal"}
        self.native.credentials = copy.deepcopy(original)
        self.enroll()
        self.client.sync(take_over_native_store=True)
        result = self.client.leave()
        self.assertTrue(result["recovery_retained"])
        self.assertEqual(self.client.status()["state"], "left")
        self.assertFalse(self.native.secrets)
        recovered = self.restart()
        with self.assertRaises(client.PausedError):
            recovered.sync()
        with self.assertRaises(client.ClientError):
            recovered.resume()
        with self.assertRaisesRegex(client.ClientError, "Complete leave --restore"):
            recovered.enroll("https://pool.example.invalid", "Test Mac", INVITE)
        result = recovered.leave(restore=True)
        self.assertTrue(result["native_restored"])
        self.assertEqual(self.native.credentials, original)
        self.assertFalse(self.path.exists())

    def test_daemon_uses_bounded_backoff_and_exits_on_revocation(self):
        self.enroll()
        self.client.sync()
        delays = []
        self.transport.error = client.RequestError(503)

        def sleep(delay):
            delays.append(delay)
            self.now += delay
            if len(delays) == 9:
                self.transport.error = client.RequestError(401)

        output = io.StringIO()
        with patch("sys.stdout", output):
            result = client.run(self.client, sleep=sleep, jitter=lambda low, high: 1.0)
        self.assertEqual(result, 2)
        self.assertEqual(delays[:3], [5, 10, 20])
        self.assertTrue(all(5 <= delay <= 300 for delay in delays))
        for secret in (INVITE, DEVICE_TOKEN, PROVIDER_TOKEN):
            self.assertNotIn(secret, output.getvalue())

    def test_service_contains_no_tokens_and_uses_fixed_absolute_interpreter(self):
        self.enroll()
        self.client.sync()
        home = Path(self.tmp.name) / "service-home"
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        with patch.object(client.sys, "platform", "darwin"):
            client.install_service(self.client, home=home, runner=runner, python="/opt/pilot/bin/python")
            path = client.service_path(home)
            data = path.read_bytes()
            value = plistlib.loads(data)
            self.assertEqual(value["ProgramArguments"][0], "/opt/pilot/bin/python")
            self.assertEqual(value["ProgramArguments"][-1], "run")
            self.assertNotIn("EnvironmentVariables", value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for secret in (INVITE, DEVICE_TOKEN, PROVIDER_TOKEN):
                self.assertNotIn(secret.encode(), data)
            client.remove_service(home=home, runner=runner)
            self.assertFalse(path.exists())
        self.assertEqual(calls[0][1], "bootstrap")
        self.assertEqual(calls[1][1], "bootout")

    def test_loaded_local_selector_blocks_enrollment_before_invitation_redemption(self):
        self.client.preflight = client.native_preflight
        with patch.object(client.sys, "platform", "darwin"), patch.object(client.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(client.ClientError, "cswap"):
                self.enroll()
        self.assertEqual(self.transport.calls, [])


class InputAndTransportTests(unittest.TestCase):
    def test_wrapper_preflight_accepts_grouped_unsets_and_rejects_changed_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            system = Path(directory) / "system"
            profiles = home / "Library/Application Support/Claude-3p/configLibrary"
            profiles.mkdir(parents=True)
            system.mkdir()
            (profiles / "_meta.json").write_text(json.dumps({"appliedId": "test-profile"}))
            profile = profiles / "test-profile.json"
            profile.write_text(json.dumps({"claudeCodeProcessWrapperEnabled": True}))
            launcher = system / "native-subscription-launcher"
            launcher.write_text(
                '#!/bin/sh\nif [ "$(/usr/bin/id -u)" = "' + str(os.getuid()) + '" ]; then\n'
                'unset CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST CLAUDE_CODE_HOST_AUTH_ENV_VAR\n'
                'unset CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR\n'
                'unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL\nfi\nexec "$@"\n'
            )
            launcher.chmod(0o755)
            (system / "managed-settings.json").write_text(json.dumps({"processWrapper": json.dumps([str(launcher)])}))
            client.verify_desktop_wrapper(home=home, system_dir=system)
            profile.write_text(json.dumps({"claudeCodeProcessWrapperEnabled": False}))
            with self.assertRaisesRegex(client.ClientError, "selected Desktop profile"):
                client.verify_desktop_wrapper(home=home, system_dir=system)
            (profiles / "_meta.json").write_text(json.dumps({"appliedId": "../../outside"}))
            with self.assertRaises(client.ClientError):
                client.verify_desktop_wrapper(home=home, system_dir=system)

    def test_origin_validation_rejects_credential_leaks_and_insecure_hosts(self):
        for value in ("http://pool.example.invalid", "https://user:pass@example.invalid",
                      "https://example.invalid/path", "https://example.invalid?token=x",
                      "https://example.invalid#token", "https://example.invalid\\@evil.invalid",
                      "https://example.invalid\n", "file:///tmp/socket", "https://example.invalid:99999"):
            with self.subTest(value=value), self.assertRaises(client.ClientError):
                client.server_url(value, allow_loopback_http=True)
        self.assertEqual(client.server_url("https://pool.example.invalid/"), "https://pool.example.invalid")
        self.assertEqual(client.server_url("http://127.0.0.1:8750", True), "http://127.0.0.1:8750")
        with self.assertRaises(client.ClientError):
            client.server_url("http://127.0.0.1:8750")

    def test_secret_files_refuse_symlinks_and_broad_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invitation"
            path.write_text(INVITE)
            path.chmod(0o644)
            with self.assertRaises(client.ClientError):
                client._private_read(path)
            path.chmod(0o600)
            self.assertEqual(client._private_read(path).decode(), INVITE)
            symlink = path.with_name("link")
            symlink.symlink_to(path)
            with self.assertRaises(client.ClientError):
                client._private_read(symlink)

    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:" + str(server.server_port)

    def test_redirect_is_not_followed_or_given_device_bearer(self):
        reached = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                reached.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/stolen")
                self.end_headers()

            def log_message(self, *args):
                pass

        transport = client.Transport(self.start_server(Handler), allow_loopback_http=True)
        with self.assertRaises(client.RequestError) as error:
            transport.post("/v1/assignment", {}, token=DEVICE_TOKEN)
        self.assertEqual(error.exception.status, 307)
        self.assertEqual(reached, ["/v1/assignment"])

    def test_error_body_and_large_response_never_escape_as_diagnostics(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(500 if self.path == "/v1/enroll" else 200)
                self.end_headers()
                try:
                    self.wfile.write((PROVIDER_TOKEN * 10000).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The client intentionally closes oversized/error bodies.

            def log_message(self, *args):
                pass

        transport = client.Transport(self.start_server(Handler), allow_loopback_http=True)
        for path in ("/v1/enroll", "/v1/assignment"):
            with self.assertRaises(client.RequestError) as error:
                transport.post(path, {}, token=DEVICE_TOKEN)
            self.assertNotIn(PROVIDER_TOKEN, str(error.exception))


if __name__ == "__main__":
    unittest.main()
