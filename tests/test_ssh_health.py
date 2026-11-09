"""SSH diagnosis does not confuse GUI readiness with helper authentication."""

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from team_pool.client import Client, SERVICE
from team_pool.native import NativeKeychainAccessError, NativeStoreError
from team_pool.ssh_health import doctor


SECRET = "synthetic-token-email-command-line-must-not-be-logged"


class GuardedNative:
    def __init__(self):
        self.guarded = False
        self.reads = 0
        self.error = None
        self.guard_error = None
        self.present = True

    @contextmanager
    def noninteractive(self):
        if self.guard_error:
            raise self.guard_error
        self.guarded = True
        try:
            yield
        finally:
            self.guarded = False

    def snapshot(self):
        if not self.guarded:
            raise AssertionError("An unguarded Keychain read is forbidden")
        self.reads += 1
        if self.error:
            raise self.error
        return {"has_credentials": self.present, "fingerprint": SECRET,
                "has_backup": True, "access_token": SECRET}


class SSHHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "team.json"
        self.state = {
            "version": 1, "server": "https://pool.example.invalid",
            "device_id": "device-fixture", "account_email": SECRET,
            "state": "active", "managed": True, "fingerprint": SECRET,
            "expires_at": 4000, "synced_at": 990, "renew_after": 30,
        }
        self.native = GuardedNative()
        self.client = Client(self.config, native=self.native, clock=lambda: 1000)
        self.domain = SimpleNamespace(returncode=0, stdout=None)
        self.service = SimpleNamespace(returncode=0, stdout=b"job = {\n\tstate = running\n}\n")
        self.calls = []
        self.bridge_health = Mock(side_effect=RuntimeError("fixture bridge unavailable"))
        self.write_state()

    def write_state(self):
        self.config.write_text(json.dumps(self.state))
        self.config.chmod(0o600)

    def runner(self, args, **kwargs):
        self.calls.append((args, kwargs))
        target = args[-1]
        result = self.domain if target == "gui/506" else self.service
        if isinstance(result, Exception):
            raise result
        return result

    def diagnose(self):
        before = self.config.read_bytes() if self.config.exists() else None
        filenames = sorted(p.name for p in self.config.parent.iterdir())
        with patch("team_pool.ssh_health.os", SimpleNamespace(getuid=lambda: 506)):
            result = doctor(self.client, runner=self.runner, platform="darwin", bridge_health=self.bridge_health)
        self.assertEqual(before, self.config.read_bytes() if self.config.exists() else None)
        self.assertEqual(filenames, sorted(p.name for p in self.config.parent.iterdir()))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertTrue(result["read_only"])
        self.assertEqual(result["checks"]["remote_helper"],
                         {"status": "not_performed", "authenticated": None})
        self.assertFalse(self.native.guarded)
        for args, kwargs in self.calls:
            self.assertEqual(args[:2], ["/bin/launchctl", "print"])
            self.assertEqual(kwargs["timeout"], 3)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        return result

    def test_missing_gui_returns_before_any_native_read(self):
        self.domain.returncode = 112
        self.native.error = AssertionError("No GUI domain: must not touch Keychain")
        result = self.diagnose()
        self.assertEqual(result["category"], "gui_session_missing")
        self.assertEqual(self.native.reads, 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("same macOS user", " ".join(result["guidance"]))
        self.assertIn("stay logged in", " ".join(result["guidance"]))

    def test_current_uid_gui_is_used_without_console_user_assumption(self):
        result = self.diagnose()
        self.assertEqual([call[0][-1] for call in self.calls],
                         ["gui/506", "gui/506/" + SERVICE])
        self.assertEqual(result["category"], "local_checks_passed_helper_unverified")
        self.assertEqual(self.calls[0][1]["stdout"], subprocess.DEVNULL)
        self.assertIn("separate macOS Keychain context", " ".join(result["guidance"]))

    def test_service_missing_and_stopped_are_distinct(self):
        for code, output, category in (
            (113, b"", "sync_service_missing"),
            (0, b"job = {\n\tstate = not running\n}\n", "sync_service_stopped"),
            (0, b"job = {\n\tstate = waiting\n}\n", "sync_service_stopped"),
        ):
            with self.subTest(category=category):
                self.service = SimpleNamespace(returncode=code, stdout=output)
                self.assertEqual(self.diagnose()["category"], category)

    def test_expired_assignment_report_has_only_safe_timestamp(self):
        self.state["expires_at"] = 999
        self.write_state()
        result = self.diagnose()
        self.assertEqual(result["category"], "assignment_expired")
        self.assertEqual(result["checks"]["assignment"],
                         {"status": "expired", "expires_at": "1970-01-01T00:16:39Z"})

    def test_manual_pause_is_not_automatically_resumed(self):
        self.state.update(state="paused", reason="native_credentials_changed")
        self.write_state()
        self.service.stdout = b"job = {\n\tstate = not running\n}\n"
        result = self.diagnose()
        self.assertEqual(result["category"], "client_paused")
        self.assertEqual(result["checks"]["client"]["pause_reason"], "native_credentials_changed")
        self.assertIn("Do not overwrite a manual login", " ".join(result["guidance"]))

    def test_unknown_pause_reason_is_redacted(self):
        for reason in (SECRET, {"access_token": SECRET}):
            self.state.update(state="paused", reason=reason)
            self.write_state()
            result = self.diagnose()
            self.assertEqual(result["checks"]["client"]["pause_reason"], "unknown")

    def test_caller_keychain_failure_is_distinct_from_gui_service_health(self):
        self.native.error = NativeKeychainAccessError(-25308)
        result = self.diagnose()
        self.assertEqual(result["category"], "caller_keychain_access_required")
        self.assertEqual(result["checks"]["gui_session"]["status"], "present")
        self.assertEqual(result["checks"]["sync_service"]["status"], "running")
        self.assertEqual(result["checks"]["caller_keychain"]["status"], "access_required")
        self.assertIn("Do not reconnect pooled accounts", " ".join(result["guidance"]))

    def test_ready_gui_bridge_does_not_require_ssh_caller_keychain(self):
        self.bridge_health.side_effect = None
        self.bridge_health.return_value = {"ok": True, "native_ready": True, "draining": False}
        self.native.error = AssertionError("SSH caller must not read native credentials")
        result = self.diagnose()
        self.assertEqual(result["category"], "gui_bridge_ready_helper_unverified")
        self.assertEqual(self.native.reads, 0)
        self.assertEqual(result["checks"]["caller_keychain"]["status"], "not_required_gui_bridge")

    def test_unready_gui_bridge_never_claims_remote_readiness(self):
        self.bridge_health.side_effect = None
        self.bridge_health.return_value = {"ok": True, "native_ready": False, "draining": False}
        self.assertEqual(self.diagnose()["category"], "gui_bridge_pool_unavailable")
        self.assertEqual(self.native.reads, 1)

    def test_guard_failure_never_retries_interactively(self):
        self.native.guard_error = NativeStoreError(SECRET)
        result = self.diagnose()
        self.assertEqual(result["category"], "caller_keychain_unavailable")
        self.assertEqual(self.native.reads, 0)

    def test_raw_keychain_error_does_not_leak(self):
        self.native.error = RuntimeError(SECRET)
        self.assertEqual(self.diagnose()["category"], "caller_keychain_unavailable")

    def test_keychain_pause_reports_foreground_recovery_without_remote_claim(self):
        self.state.update(state="paused", reason="keychain_access_required")
        self.write_state()
        self.assertEqual(self.diagnose()["category"], "client_paused")

    def test_probe_timeout_or_permission_failure_is_not_reported_as_logout(self):
        for error in (subprocess.TimeoutExpired(SECRET, 3, output=SECRET),
                      OSError(SECRET), SimpleNamespace(returncode=1, stdout=SECRET)):
            self.domain = error
            result = self.diagnose()
            self.assertEqual(result["category"], "gui_session_unknown")
            self.assertEqual(self.native.reads, 0)

    def test_launchctl_details_are_not_returned_and_unknown_or_large_output_is_rejected(self):
        self.service.stdout = ("job = {\n\tstate = running\n\tenvironment = {\n\t\tTOKEN = " + SECRET + "\n\t}\n}\n").encode()
        self.assertEqual(self.diagnose()["category"], "local_checks_passed_helper_unverified")
        for raw in (SECRET.encode(), b"x" * (65536 + 1),
                    b"job = {\n\tstate = running\n\tstate = waiting\n}\n",
                    b"job = {\n\tstate = future new state\n}\n"):
            self.service.stdout = raw
            self.assertEqual(self.diagnose()["category"], "sync_service_unknown")

    def test_nested_launchctl_resource_states_do_not_hide_running_service(self):
        self.service.stdout = ("""gui/506/study.paradigm.claude-desktop-swap.team = {
	active count = 1
	path = /private/fixture/agent.plist
	type = LaunchAgent
	state = running
	environment = {
		TOKEN = """ + SECRET + """
	}
	resource coalition = {
		ID = 123
		type = resource
		state = active
		active count = 1
	}
	jetsam coalition = {
		ID = 124
		type = jetsam
		state = active
	}
}
""").encode()
        result = self.diagnose()
        self.assertEqual(result["category"], "local_checks_passed_helper_unverified")
        self.assertEqual(result["checks"]["sync_service"],
                         {"status": "running", "launchd_state": "running"})
        # A nested running state must not replace a stopped top-level service.
        self.service.stdout = self.service.stdout.replace(b"\tstate = running", b"\tstate = not running").replace(b"\t\tstate = active", b"\t\tstate = running")
        self.assertEqual(self.diagnose()["category"], "sync_service_stopped")

    def test_missing_or_malformed_assignment_is_not_claimed_healthy(self):
        for value in (None, True, SECRET, float("inf"), float("nan"), 1e100, 10 ** 1000):
            self.state["expires_at"] = value
            self.write_state()
            result = self.diagnose()
            self.assertEqual(result["category"], "assignment_unknown")
            self.assertEqual(result["checks"]["assignment"], {"status": "unknown"})

    def test_missing_credentials_are_reported_without_token_or_fingerprint(self):
        self.native.present = False
        result = self.diagnose()
        self.assertEqual(result["category"], "native_credential_missing")
        self.assertEqual(result["checks"]["caller_keychain"],
                         {"status": "readable", "has_credentials": False})

    def test_pending_write_and_inactive_enrollment_are_not_resumed(self):
        self.state["pending_write"] = {"access_token": SECRET}
        self.write_state()
        self.assertEqual(self.diagnose()["category"], "native_write_recovery_pending")
        for status in ("left", "revoked"):
            self.state["state"] = status
            self.write_state()
            self.assertEqual(self.diagnose()["category"], "enrollment_inactive")

    def test_not_enrolled_or_invalid_file_never_reads_keychain(self):
        self.config.unlink()
        self.assertEqual(self.diagnose()["category"], "not_enrolled")
        self.config.write_text(SECRET)
        self.config.chmod(0o600)
        self.assertEqual(self.diagnose()["category"], "client_state_unreadable")
        self.assertEqual(self.native.reads, 0)

    def test_non_macos_and_root_do_not_probe_other_users(self):
        runner = Mock(side_effect=AssertionError("No launchctl probe allowed"))
        self.assertEqual(doctor(self.client, runner=runner, platform="linux")["category"], "unsupported_platform")
        with patch("team_pool.ssh_health.os", SimpleNamespace(getuid=lambda: 0)):
            self.assertEqual(doctor(self.client, runner=runner, platform="darwin")["category"], "wrong_user")
        runner.assert_not_called()
        self.assertEqual(self.native.reads, 0)


if __name__ == "__main__":
    unittest.main()
