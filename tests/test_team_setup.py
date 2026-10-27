"""One-command setup preserves native ownership and validates background success."""
import copy
import io
import itertools
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from team_pool.client import Client, ClientError, DEVICE_SECRET, SERVICE, service_path
from team_pool.setup import Setup, TEAM_ID, directory_access_needs_repair, inspect_state, setup_lock, share_membership
from team_pool.share_enrollment import DEFAULT_SHARE_SERVER
from team_pool.desktop_profile import DesktopProfileError
from test_team_client import FakeNative, FakeTransport


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.source = self.home / "release"
        (self.source / "scripts").mkdir(parents=True)
        for name in ("desktop_swap.py", "install_team_status.py"):
            (self.source / "scripts" / name).write_text("# fixture\n")
        self.auth = self.home / "account.json"
        self.auth.write_text(json.dumps({"servers": {DEFAULT_SHARE_SERVER: {"token": "ssa_" + "x" * 32}}}))
        self.auth.chmod(0o600)
        self.native = FakeNative({"original": "subscription"})
        self.transport = FakeTransport()
        self.client = Client(self.home / "state/team.json", native=self.native,
                             transport=self.transport, preflight=lambda: None,
                             clock=lambda: next(self.ticks))
        self.ticks = itertools.count(1000)
        self.commands = []
        self.loaded = False
        self.bootout_fails = False
        self.process_lingers = False
        self.membership = Mock(return_value="account001")
        self.enroll_calls = []
        self.check_wrapper = Mock(return_value=None)
        self.desktop_preflight = Mock(return_value=None)
        self.access_check = Mock(return_value=False)
        self.output = []
        self.setup = Setup(self.source, client=self.client, account_file=self.auth,
                           home=self.home, runner=self.runner, membership=self.membership,
                           enroll=self.enroll, installer=self.install,
                           verify_wrapper=self.check_wrapper, output=self.output.append,
                           desktop_preflight=self.desktop_preflight,
                           access_check=self.access_check,
                           sleep=lambda _: None, clock=lambda: next(self.ticks))
        self.platform = patch("team_pool.setup.sys.platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def runner(self, args, **kwargs):
        self.commands.append(args)
        code, out, err = 0, "", ""
        if args[:2] == ["/bin/launchctl", "print"]:
            if args[-1].endswith(SERVICE) and self.loaded:
                out = f"path = {service_path(self.home)}\npid = 12345\n"
            else:
                code, err = 113, "Could not find service"
        elif args[:2] == ["/bin/launchctl", "bootout"]:
            if self.bootout_fails:
                code = 1
            else:
                self.loaded = False
        elif args[:3] == ["/bin/ps", "-p", "12345"]:
            code = 0 if self.process_lingers else 1
        elif args[0] == "/usr/bin/pgrep":
            code = 1
        return subprocess.CompletedProcess(args, code, out, err)

    def enroll(self, client, *, link, **kwargs):
        self.enroll_calls.append(link)
        state = client._load(optional=True)
        if not link:
            state = {"version": 1, "state": "enrolled", "managed": False,
                     "server": "https://pool.example.test", "device_id": "same-device"}
            client.native.set_secret(DEVICE_SECRET, "same-secret")
        state.update(share_account_id="account001", share_team_id=TEAM_ID, share_team_name="Paradigm")
        client._save(state)

    def install(self, client, **kwargs):
        path = service_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({"Label": SERVICE, "ProgramArguments": [
            "/release/venv/bin/python", "-m", "team_pool.client", "--config", str(client.config), "run"]}))
        path.chmod(0o600)
        self.loaded = True
        with client.native.noninteractive():
            client.sync()

    def active(self, **changes):
        self.enroll(self.client, link=False)
        self.client.sync(take_over_native_store=True)
        state = self.client._load()
        state.update(changes)
        self.client._save(state)
        self.enroll_calls.clear()

    def mutations(self):
        return [args for args in self.commands if args[0] in ("/usr/bin/sudo", "/usr/bin/osascript")
                or args[:2] in (["/bin/launchctl", "bootout"], ["/bin/launchctl", "disable"], ["/bin/launchctl", "enable"])]

    def test_fresh_setup_takes_over_once_and_checks_background(self):
        original = copy.deepcopy(self.native.credentials)
        result = self.setup.run()
        self.assertEqual(result["state"], "active")
        self.assertEqual(self.native.original, original)
        self.assertEqual(self.enroll_calls, [False])
        self.assertTrue(self.loaded)
        self.assertFalse(any(args[0] in ("/usr/bin/sudo", "/usr/bin/osascript") for args in self.commands))
        self.assertIn("background sync was verified", self.output[-1])

    def test_directory_repair_precedes_native_reads_without_stopping_desktop(self):
        self.access_check.side_effect = [True, False]
        original_preflight = self.native.preflight
        def preflight():
            self.assertTrue(any("repair-access" in args for args in self.commands))
            self.assertFalse(any(args[0] == "/usr/bin/osascript" for args in self.commands))
            return original_preflight()
        self.native.preflight = preflight
        self.setup.run()
        repair = [args for args in self.commands if "repair-access" in args]
        self.assertEqual(len(repair), 1)
        self.assertEqual(repair[0][0], "/usr/bin/sudo")
        self.membership.assert_called_once_with(self.auth)
        self.assertEqual(self.client._load()["state"], "active")

    def test_directory_repair_check_explains_filesystem_failure_without_keychain(self):
        self.access_check.return_value = True
        self.native.preflight = Mock(side_effect=AssertionError("Keychain must not be reached"))
        with self.assertRaisesRegex(ClientError, "directory unreadable"):
            self.setup.run(check=True)
        self.assertEqual(self.mutations(), [])
        self.native.preflight.assert_not_called()
        self.membership.assert_not_called()

    def test_membership_failure_prevents_directory_repair(self):
        self.access_check.return_value = True
        self.membership.side_effect = ClientError("not a member")
        with self.assertRaisesRegex(ClientError, "not a member"):
            self.setup.run()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.native.writes, [])

    def test_failed_directory_repair_does_not_continue_into_native_reads(self):
        self.access_check.return_value = True
        self.native.preflight = Mock(side_effect=AssertionError("Keychain must not be reached"))
        original_runner = self.runner
        def runner(args, **kwargs):
            result = original_runner(args, **kwargs)
            if "repair-access" in args:
                result.returncode = 1
            return result
        self.setup.runner = runner
        with self.assertRaisesRegex(ClientError, "permissions could not be repaired"):
            self.setup.run()
        self.native.preflight.assert_not_called()
        self.desktop_preflight.assert_not_called()
        self.assertEqual(self.enroll_calls, [])
        self.assertFalse(any(args[0] == "/usr/bin/osascript" for args in self.commands))

    def test_rerun_preserves_device_backup_and_uses_link(self):
        self.setup.run()
        before = self.client._load()
        backup = copy.deepcopy(self.native.original)
        self.setup.run()
        after = self.client._load()
        self.assertEqual(self.enroll_calls, [False, True])
        self.assertEqual(before["device_id"], after["device_id"])
        self.assertEqual(self.native.secrets[DEVICE_SECRET], "same-secret")
        self.assertEqual(self.native.original, backup)

    def test_invite_device_is_linked_without_new_device(self):
        self.active()
        state = self.client._load()
        for key in ("share_account_id", "share_team_id", "share_team_name"):
            state.pop(key)
        self.client._save(state)
        self.setup.run()
        self.assertEqual(self.enroll_calls, [True])
        self.assertEqual(self.client._load()["device_id"], "same-device")

    def test_membership_failure_happens_before_disruption(self):
        self.membership.side_effect = ClientError("not a member")
        with self.assertRaisesRegex(ClientError, "not a member"):
            self.setup.run()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.enroll_calls, [])
        self.assertEqual(self.native.writes, [])

    def test_uninitialized_desktop_fails_before_prompts_or_service_stop(self):
        self.active()
        self.install(self.client)
        self.commands.clear()
        self.native.writes.clear()
        self.desktop_preflight.side_effect = DesktopProfileError("Open Claude Desktop and finish its first sign-in")
        with self.assertRaisesRegex(ClientError, "first sign-in"):
            self.setup.run()
        self.assertTrue(self.loaded)
        self.assertEqual(self.output, [])
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.enroll_calls, [])
        self.assertEqual(self.native.writes, [])
        self.membership.assert_not_called()

    def test_changed_share_identity_is_rejected_before_disruption(self):
        self.active()
        self.membership.return_value = "different-account"
        with self.assertRaisesRegex(ClientError, "different Claude Share identity"):
            self.setup.run()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.enroll_calls, [])

    def test_paused_revoked_left_and_pending_write_are_not_resumed(self):
        for values in ({"state": "paused"}, {"state": "revoked"}, {"state": "left"}, {"pending_write": {"before": "pending"}}):
            with self.subTest(values=values):
                self.active(**values)
                before = self.client.config.read_bytes()
                with self.assertRaises(ClientError):
                    self.setup.run()
                self.assertEqual(self.client.config.read_bytes(), before)
                self.assertEqual(self.mutations(), [])

    def test_manual_native_drift_is_not_overwritten(self):
        self.active()
        self.native.credentials = {"manual": "new account"}
        before = copy.deepcopy(self.native.credentials)
        with self.assertRaisesRegex(ClientError, "manual login"):
            self.setup.run()
        self.assertEqual(self.native.credentials, before)
        self.assertEqual(self.mutations(), [])

    def test_failed_service_stop_keeps_service_file_and_enrollment(self):
        self.active()
        self.install(self.client)
        before = service_path(self.home).read_bytes()
        self.bootout_fails = True
        with self.assertRaisesRegex(ClientError, "could not stop"):
            self.setup.run()
        self.assertEqual(service_path(self.home).read_bytes(), before)
        self.assertEqual(self.enroll_calls, [])

    def test_bootout_must_wait_for_old_process_to_exit(self):
        self.active()
        self.install(self.client)
        self.process_lingers = True
        with self.assertRaisesRegex(ClientError, "did not stop"):
            self.setup.run()
        self.assertTrue(service_path(self.home).exists())
        self.assertEqual(self.enroll_calls, [])

    def test_stopped_service_file_survives_later_enrollment_failure(self):
        self.active()
        self.install(self.client)
        before = service_path(self.home).read_bytes()
        self.setup.enroll = Mock(side_effect=ClientError("membership expired"))
        with self.assertRaisesRegex(ClientError, "membership expired"):
            self.setup.run()
        self.assertFalse(self.loaded)
        self.assertEqual(service_path(self.home).read_bytes(), before)
        self.assertTrue(any("Background synchronization is stopped" in line for line in self.output))

    def test_replacement_bootstrap_failure_restores_previous_service_file(self):
        self.active()
        self.install(self.client)
        before = service_path(self.home).read_bytes()
        self.setup.installer = Mock(side_effect=ClientError("replacement failed"))
        with self.assertRaisesRegex(ClientError, "replacement failed"):
            self.setup.run()
        self.assertEqual(service_path(self.home).read_bytes(), before)
        self.assertFalse(self.loaded)
        self.assertTrue(any("configuration was restored" in line for line in self.output))

    def test_failed_wrapper_install_reopens_previously_running_desktop(self):
        self.check_wrapper.side_effect = ClientError("not installed")
        original_runner = self.runner
        running = [True]
        def runner(args, **kwargs):
            response = original_runner(args, **kwargs)
            if args[0] == "/usr/bin/pgrep":
                response.returncode = 0 if running[0] else 1
            if args[0] == "/usr/bin/osascript":
                running[0] = False
            if args[0] == "/usr/bin/sudo":
                response.returncode = 1
            return response
        self.setup.runner = runner
        with self.assertRaisesRegex(ClientError, "installation did not complete"):
            self.setup.run()
        self.assertIn(["/usr/bin/open", "-b", "com.anthropic.claudefordesktop"], self.commands)
        self.assertEqual(self.enroll_calls, [])

    def test_desktop_quit_refusal_never_forces_kill_or_installs(self):
        self.check_wrapper.side_effect = ClientError("not installed")
        original_runner = self.runner
        def runner(args, **kwargs):
            response = original_runner(args, **kwargs)
            if args[0] == "/usr/bin/pgrep":
                response.returncode = 0
            if args[0] == "/usr/bin/osascript":
                response.returncode = 1
            return response
        self.setup.runner = runner
        with self.assertRaisesRegex(ClientError, "could not quit"):
            self.setup.run()
        self.assertFalse(any(args[0] in ("/usr/bin/sudo", "/bin/kill", "/usr/bin/killall") for args in self.commands))
        self.assertEqual(self.enroll_calls, [])

    def test_background_validation_waits_for_launchctl_to_allocate_pid(self):
        self.active()
        state = self.client._load()
        state["synced_at"] += 1
        self.client._save(state)
        self.setup.loaded = Mock(side_effect=["state = spawn scheduled\n", "pid = 12345\n"])
        self.setup.background_ready(state["synced_at"] - 1)
        self.assertEqual(self.setup.loaded.call_count, 2)

    def test_background_exit_before_first_poll_is_failure(self):
        self.active()
        self.setup.loaded = Mock(return_value="state = not running\nlast exit code = 2\n")
        with self.assertRaisesRegex(ClientError, "process exited"):
            self.setup.background_ready(self.client._load()["synced_at"])

    def test_background_pause_does_not_report_success(self):
        def paused(client, **kwargs):
            self.install(client)
            state = client._load()
            state.update(state="paused", reason="keychain_access_required")
            client._save(state)
        self.setup.installer = paused
        with self.assertRaisesRegex(ClientError, "Background sync stopped or paused"):
            self.setup.run()
        self.assertFalse(any("Setup complete" in message for message in self.output))

    def test_readiness_check_never_enrolls_or_changes_state(self):
        self.check_wrapper.side_effect = ClientError("missing")
        before = set(self.home.rglob("*"))
        result = self.setup.run(check=True)
        self.assertFalse(result["wrapper_ready"])
        self.assertEqual(set(self.home.rglob("*")), before)
        self.membership.assert_not_called()
        self.assertEqual(self.enroll_calls, [])
        self.assertEqual(self.mutations(), [])

    def test_setup_lock_rejects_concurrent_run(self):
        path = self.home / "private/setup.lock"
        with setup_lock(path):
            with self.assertRaisesRegex(ClientError, "Another setup"):
                with setup_lock(path):
                    self.fail("second lock entered")

    def test_authoritative_membership_checks_exact_team_and_identity(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, size):
                return json.dumps({"account": {"id": "account001"}, "teams": [
                    {"id": TEAM_ID, "name": "Paradigm", "role": "member"}]}).encode()
        transport = Mock()
        transport.opener.open.return_value = Response()
        self.assertEqual(share_membership(self.auth, transport=transport), "account001")
        request = transport.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, DEFAULT_SHARE_SERVER + "/api/me")
        self.assertIsNone(request.data)
        self.assertNotIn("ssa_", request.full_url)


class DirectoryAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.system = self.root / "ClaudeCode"
        self.managed = self.root / "Managed Preferences"

    def check(self):
        return directory_access_needs_repair(system_dir=self.system, managed_root=self.managed,
                                             username="fixture", system_uid=os.getuid())

    def test_missing_directories_need_no_repair(self):
        self.assertFalse(self.check())

    def test_private_directory_is_detected_without_changing_it(self):
        self.system.mkdir(mode=0o700)
        self.assertTrue(self.check())
        self.assertEqual(self.system.stat().st_mode & 0o777, 0o700)
        self.system.chmod(0o755)
        self.assertFalse(self.check())

    def test_private_scoped_policy_directory_is_detected(self):
        self.managed.mkdir(mode=0o755)
        self.managed.chmod(0o755)
        (self.managed / "fixture").mkdir(mode=0o700)
        self.assertTrue(self.check())

    def test_unsafe_or_symlinked_directory_is_not_treated_as_legacy_repair(self):
        self.system.mkdir(mode=0o755)
        self.system.chmod(0o777)
        with self.assertRaisesRegex(ClientError, "unsafe ownership or permissions"):
            self.check()
        self.system.rmdir()
        self.managed.mkdir(mode=0o755)
        self.system.symlink_to(self.managed)
        with self.assertRaisesRegex(ClientError, "unsafe ownership or permissions"):
            self.check()


if __name__ == "__main__":
    unittest.main()
