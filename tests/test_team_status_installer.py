"""Installer must leave unrelated apps and LaunchAgents untouched."""
import importlib.util
import json
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "status_installer", Path(__file__).resolve().parents[1] / "scripts/install_team_status.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class StatusInstallerOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.app, self.agent = installer.paths(self.home)

    def test_install_refuses_unrecognized_app_before_build_or_service_access(self):
        self.app.mkdir(parents=True)
        sentinel = self.app / "unrelated-user-file"
        sentinel.write_text("keep")
        with patch.object(installer, "build_bundle") as build, patch.object(installer, "launchctl") as ctl:
            with self.assertRaisesRegex(installer.InstallError, "unrecognized app"):
                installer.install(self.home)
        build.assert_not_called()
        ctl.assert_not_called()
        self.assertEqual(sentinel.read_text(), "keep")

    def test_uninstall_refuses_launch_agent_with_different_command(self):
        self.agent.parent.mkdir(parents=True)
        original = plistlib.dumps({**installer.specification(self.app), "ProgramArguments": ["/unrelated"]})
        self.agent.write_bytes(original)
        with patch.object(installer, "launchctl") as ctl:
            with self.assertRaisesRegex(installer.InstallError, "unrecognized LaunchAgent"):
                installer.uninstall(self.home)
        ctl.assert_not_called()
        self.assertEqual(self.agent.read_bytes(), original)

    def test_uninstall_refuses_symlink_even_to_expected_launch_agent(self):
        self.agent.parent.mkdir(parents=True)
        target = self.home / "other.plist"
        target.write_bytes(plistlib.dumps(installer.specification(self.app)))
        self.agent.symlink_to(target)
        with patch.object(installer, "launchctl") as ctl:
            with self.assertRaises(installer.InstallError):
                installer.uninstall(self.home)
        ctl.assert_not_called()
        self.assertTrue(self.agent.is_symlink())
        self.assertTrue(target.exists())

    def test_uninstall_does_not_stop_unowned_loaded_service(self):
        with patch.object(installer, "loaded", return_value=True), patch.object(installer, "stop") as stop:
            self.assertEqual(installer.uninstall(self.home), {"installed": False})
        stop.assert_not_called()

    def bundle(self, helper=None):
        resources = self.app / "Contents/Resources"
        resources.mkdir(parents=True)
        binary = self.app / "Contents/MacOS" / installer.EXECUTABLE
        binary.parent.mkdir()
        binary.write_text("fixture")
        (resources / "claude-swap-status.json").write_text(json.dumps(installer.MARKER))
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": installer.LABEL, "CFBundleExecutable": installer.EXECUTABLE}))
        if helper is not None:
            (resources / "helper.json").write_text(json.dumps(helper))

    def test_existing_read_only_bundle_is_recognized_for_upgrade(self):
        self.bundle()
        installer.verify_app(self.app)

    def test_old_owned_helper_path_can_be_repaired_when_runtime_is_gone(self):
        self.bundle({"python": "/retired-release/bin/python", "module": installer.HELPER_MODULE, "version": 1})
        installer.verify_app(self.app)

    def test_unknown_helper_module_is_rejected_before_service_changes(self):
        self.bundle({"python": sys.executable, "module": "unrelated.module", "version": 1})
        with patch.object(installer, "launchctl") as ctl:
            with self.assertRaisesRegex(installer.InstallError, "unrecognized app"):
                installer.install(self.home)
        ctl.assert_not_called()

    def test_writable_helper_is_rejected(self):
        self.bundle({"python": sys.executable, "module": installer.HELPER_MODULE, "version": 1})
        (self.app / "Contents/Resources/helper.json").chmod(0o666)
        with self.assertRaisesRegex(installer.InstallError, "unrecognized app"):
            installer.verify_app(self.app)

    def test_helper_preserves_virtualenv_python_path(self):
        executable = self.home / "fixture-python"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        runtime = self.home / "release/venv/bin/python"
        runtime.parent.mkdir(parents=True)
        runtime.symlink_to(executable)
        self.assertEqual(installer.helper_specification(runtime), {
            "python": str(runtime), "module": installer.HELPER_MODULE, "version": 1})

    def test_build_records_private_runtime_configuration_before_signing(self):
        executable = self.home / "fixture-python"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        destination = self.home / "New.app"
        with patch.object(installer.sys, "executable", str(executable)), \
             patch.object(installer.subprocess, "run") as run:
            installer.build_bundle(destination)
        path = destination / "Contents/Resources/helper.json"
        self.assertEqual(json.loads(path.read_text()), installer.helper_specification(executable))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(run.call_args_list[-1].args[0][0], "/usr/bin/codesign")

    def test_group_or_world_writable_runtime_is_rejected(self):
        executable = self.home / "fixture-python"
        executable.write_text("#!/bin/sh\nexit 0\n")
        for mode in (0o770, 0o702):
            with self.subTest(mode=oct(mode)):
                executable.chmod(mode)
                with self.assertRaisesRegex(installer.InstallError, "persistent Claude Swap Python runtime"):
                    installer.helper_specification(executable)

    def test_failed_menu_start_restores_previous_app_and_launch_agent(self):
        self.bundle()
        original_binary = self.app / "Contents/MacOS" / installer.EXECUTABLE
        self.agent.parent.mkdir(parents=True)
        original = plistlib.dumps(installer.specification(self.app))
        self.agent.write_bytes(original)

        def build(destination):
            destination.mkdir(parents=True)
            (destination / "new-version").write_text("staged")

        def ctl(*args):
            return subprocess.CompletedProcess(args, 1 if args[0] == "bootstrap" else 0)

        with patch.object(installer, "build_bundle", side_effect=build), \
             patch.object(installer, "loaded", return_value=False), \
             patch.object(installer, "stop"), patch.object(installer, "launchctl", side_effect=ctl):
            with self.assertRaisesRegex(installer.InstallError, "Could not start"):
                installer.install(self.home)
        self.assertEqual(original_binary.read_text(), "fixture")
        self.assertEqual(self.agent.read_bytes(), original)
        self.assertFalse((self.app / "new-version").exists())


@unittest.skipUnless(sys.platform == "darwin", "AppKit bridge integration requires macOS")
class NativeMenuBridgeTests(unittest.TestCase):
    """Run the compiled Swift transport against a fake helper, without opening UI."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="claude-menu-contract-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        source = installer.SOURCE.read_text().split("let delegate = StatusApp()", 1)[0]
        harness = r'''
let command = CommandLine.arguments[1]
if command == "format" {
    precondition(percentage(32.3) == "32%")
    precondition(percentage(true) == "—")
    precondition(percentage(Double.nan) == "—")
    let absentMeter: [String: Any] = ["five_hour": NSNull(), "seven_day": 11]
    precondition(quotaDisplay(absentMeter, key: "five_hour") == "Not reported")
    precondition(quotaDisplay(absentMeter, key: "seven_day") == "11%")
    precondition(quotaDisplay(["five_hour": 0], key: "five_hour") == "0%")
    precondition(quotaDisplay([:], key: "five_hour") == "—")
    precondition(quotaDisplay(["five_hour": 100], key: "five_hour", stale: true) == "100% (stale)")
    precondition(quotaSummary(["five_hour": 100, "seven_day": 100], stale: true).hasSuffix("stale"))
    precondition(!quotaSummary(absentMeter, stale: false).contains("0%"))
    let quotaNow = Date(timeIntervalSince1970: 1000)
    precondition(quotaIsStale(["usage": absentMeter, "fetched": 699, "reason": "quarantined"], now: quotaNow))
    precondition(quotaIsStale(["usage": absentMeter, "fetched": 1001], now: quotaNow))
    precondition(quotaIsStale(["usage": absentMeter], now: quotaNow))
    precondition(!quotaIsStale(["usage": absentMeter, "fetched": 999], now: quotaNow))
    // Losing the catalog connection does not invalidate a recently fetched quota.
    let cachedAccount: [String: Any] = ["usage": absentMeter, "fetched": 999]
    precondition(!quotaIsStale(cachedAccount, now: quotaNow))
    precondition(!catalogControlsEnabled(bridgeBusy: false, catalogFailed: true))
    precondition(quotaIsStale(cachedAccount, now: quotaNow.addingTimeInterval(300)))
    precondition(quotaIsStale(["usage": absentMeter, "fetched": 999, "reason": "quota-stale"], now: quotaNow))
    precondition(catalogControlsEnabled(bridgeBusy: false, catalogFailed: false))
    precondition(!catalogControlsEnabled(bridgeBusy: true, catalogFailed: false))
    precondition(helperTimeout(for: "list") == 120)
    precondition(helperTimeout(for: "switch") == 240)
    precondition(helperTimeout(for: "contribute-complete") == 240)
    precondition(claudeAuthorizationURL("https://claude.com/cai/oauth/authorize?state=test") != nil)
    precondition(claudeAuthorizationURL("https://claude.com.evil.test/cai/oauth/authorize") == nil)
    precondition(claudeAuthorizationURL("https://evil.test@claude.com/cai/oauth/authorize") == nil)
    precondition(claudeAuthorizationURL("https://claude.com/cai/oauth/authorize#fragment") == nil)
    print("format-ok")
} else if command == "diagnostics" {
    let directory = URL(fileURLWithPath: CommandLine.arguments[2])
    for n in 1...70 {
        recordMenuDiagnostic(n == 70 ? .recovered : .timeout,
                             now: Date(timeIntervalSince1970: Double(n)), directory: directory)
    }
    print("diagnostics-ok")
} else if command == "recovery" {
    let target = "quarantined-account"
    for state in ["pending", "submitted", "exchanging", "uncertain", "cancelled", "failed", "expired", "recovered"] {
        let receipt: [String: Any] = ["state": state, "account_id": target]
        precondition(recoveryAccountID(receipt) == target)
    }
    for state in ["added", "duplicate"] {
        let receipt: [String: Any] = ["state": state, "account_id": target]
        precondition(recoveryAccountID(receipt) == nil)
    }
    for state in ["pending"] {
        let receipt: [String: Any] = ["state": state, "account_id": target]
        precondition(canBeginRecovery(target, receipt: receipt))
        precondition(!canBeginRecovery("another-account", receipt: receipt))
        precondition(!canBeginRecovery(target, receipt: ["state": state]))
    }
    for state in ["submitted", "exchanging"] {
        precondition(!canBeginRecovery(target, receipt: ["state": state, "account_id": target]))
    }
    precondition(canBeginRecovery(target, receipt: ["state": "uncertain", "account_id": target]))
    precondition(canBeginRecovery("another-account", receipt: ["state": "uncertain", "account_id": target]))
    precondition(canBeginRecovery(target, receipt: ["state": "added", "account_id": "other"]))
    precondition(contributionNotice(["state": "cancelled", "account_id": target]) == "Account recovery cancelled")
    precondition(contributionNotice(["state": "cancelled"]) == "Account addition cancelled")
    precondition(contributionNotice(["state": "recovered", "account_id": target]).contains("reconnected to the shared pool"))
    print("recovery-ok")
} else if command == "local-status" {
    let path = URL(fileURLWithPath: CommandLine.arguments[2])
    let now = Date().timeIntervalSince1970
    var state: [String: Any] = ["state": "active", "managed": true, "account_id": "new-account",
                                "account_email": "new@example.test", "expires_at": now + 600,
                                "synced_at": now, "renew_after": 60]
    func save() { try! JSONSerialization.data(withJSONObject: state).write(to: path) }
    save()
    let active = readStatus(statePath: path, runningOverride: true)
    precondition(active.accountID == "new-account" && active.canSwitch)
    state["contribution"] = ["state": "pending", "contribution_id": "recovery-attempt", "account_id": "quarantined-account"]
    save()
    let resumed = readStatus(statePath: path, runningOverride: true)
    precondition(resumed.accountID == "new-account" && resumed.canSwitch)
    precondition(recoveryAccountID(resumed.contribution) == "quarantined-account")
    state["state"] = "paused"
    save()
    precondition(!readStatus(statePath: path, runningOverride: true).canSwitch)
    state["state"] = "active"
    state["pending_write"] = ["account_id": "other-account"]
    save()
    precondition(!readStatus(statePath: path, runningOverride: true).canSwitch)
    state.removeValue(forKey: "pending_write")
    save()
    precondition(!readStatus(statePath: path, runningOverride: false).canSwitch)
    state["synced_at"] = now - 600
    save()
    precondition(!readStatus(statePath: path, runningOverride: true).canSwitch)
    print("local-status-ok")
} else {
    do {
        let request: [String: Any] = command == "recover-start"
            ? ["action": "recover-start", "account_id": "quarantined-account"]
            : ["action": "contribute-complete", "contribution_id": "fixture-id", "code": "fixture-code#fixture-state"]
        let result = try bridge(request,
                                configurationURL: URL(fileURLWithPath: CommandLine.arguments[2]),
                                timeout: command == "timeout" ? 0.2 : 5)
        print(String(data: try JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
    } catch let failure as BridgeFailure {
        if command == "diagnostic-request" {
            recordMenuDiagnostic(failure.diagnostic,
                directory: URL(fileURLWithPath: CommandLine.arguments[2]).deletingLastPathComponent())
            print("diagnostic-recorded")
        } else { print("ERROR: " + failure.message) }
    }
      catch { print("ERROR: unexpected helper failure") }
}
'''
        swift = cls.root / "main.swift"
        swift.write_text(source + harness)
        cls.binary = cls.root / "MenuBridgeHarness"
        subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", "-framework", "AppKit", str(swift),
                        "-o", str(cls.binary)], check=True, capture_output=True, timeout=90)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def run_helper(self, body, command="request"):
        stub = self.work / "helper.py"
        stub.write_text("import json, sys, time\n" + body)
        executable = self.work / "python"
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(stub)) + ' "$@"\n')
        executable.chmod(0o700)
        config = self.work / "helper.json"
        config.write_text(json.dumps({"python": str(executable), "module": installer.HELPER_MODULE, "version": 1}))
        config.chmod(0o600)
        return subprocess.run([str(self.binary), command, str(config)], capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()

    def test_quota_format_and_official_authorization_origin(self):
        result = subprocess.run([str(self.binary), "format"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "format-ok")

    def test_catalog_diagnostics_are_private_bounded_and_record_recovery(self):
        result = subprocess.run([str(self.binary), "diagnostics", str(self.work)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "diagnostics-ok")
        path = self.work / "menu-diagnostics.json"
        entries = json.loads(path.read_text())
        self.assertEqual(len(entries), 64)
        self.assertEqual(entries[0], {"at": 7, "category": "timeout"})
        self.assertEqual(entries[-1], {"at": 70, "category": "recovered"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertLess(path.stat().st_size, 16_384)

    def test_catalog_diagnostics_refuse_link_and_shared_directory(self):
        target = self.work / "unrelated"
        target.write_text("preserve")
        path = self.work / "menu-diagnostics.json"
        path.symlink_to(target)
        subprocess.run([str(self.binary), "diagnostics", str(self.work)], check=True, capture_output=True)
        self.assertEqual(target.read_text(), "preserve")
        self.assertTrue(path.is_symlink())
        path.unlink()
        self.work.chmod(0o755)
        subprocess.run([str(self.binary), "diagnostics", str(self.work)], check=True, capture_output=True)
        self.assertFalse(path.exists())

    def test_catalog_failure_diagnostics_never_store_helper_error_or_unknown_kind(self):
        for kind, expected in [("dns", "dns"), ("private-token-value", "helper-request-failed"),
                               ("loaded", "helper-request-failed"), (None, "helper-request-failed")]:
            with self.subTest(kind=kind):
                result = self.run_helper("print(json.dumps({'ok':False,"
                    "'error':'private authorization code must not be logged',"
                    "'error_kind':" + repr(kind) + "}))\nsys.exit(1)\n", command="diagnostic-request")
                self.assertEqual(result, "diagnostic-recorded")
                path = self.work / "menu-diagnostics.json"
                entries = json.loads(path.read_text())
                self.assertEqual(entries[-1]["category"], expected)
                self.assertEqual(set(entries[-1]), {"at", "category"})
                self.assertNotIn("private", path.read_text())

    def test_helper_receives_code_only_on_stdin_and_reply_is_decoded(self):
        result = self.run_helper("request = json.load(sys.stdin)\n"
            "assert sys.argv[1:] == ['-I', '-m', 'team_pool.menu_bridge']\n"
            "assert request == {'action':'contribute-complete','contribution_id':'fixture-id','code':'fixture-code#fixture-state'}\n"
            "print(json.dumps({'ok':True,'result':{'state':'added'}}))\n")
        self.assertEqual(json.loads(result), {"state": "added"})

    def test_targeted_recovery_request_keeps_target_in_stdin_and_receipt(self):
        result = self.run_helper("request = json.load(sys.stdin)\n"
            "assert sys.argv[1:] == ['-I', '-m', 'team_pool.menu_bridge']\n"
            "assert request == {'action':'recover-start','account_id':'quarantined-account'}\n"
            "print(json.dumps({'ok':True,'result':{'state':'pending','account_id':request['account_id']}}))\n",
            command="recover-start")
        self.assertEqual(json.loads(result), {"state": "pending", "account_id": "quarantined-account"})

    def test_recovery_receipts_resume_without_changing_target_or_add_intent(self):
        result = subprocess.run([str(self.binary), "recovery"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "recovery-ok")

    def test_local_assignment_and_activity_control_manual_switching(self):
        result = subprocess.run([str(self.binary), "local-status", str(self.work / "state.json")],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "local-status-ok")

    def test_safe_error_is_returned_without_helper_stderr(self):
        result = self.run_helper("print('private diagnostic', file=sys.stderr)\n"
            "print(json.dumps({'ok':False,'error':'Account is not available.'}))\nsys.exit(1)\n")
        self.assertEqual(result, "ERROR: Account is not available.")

    def test_malformed_and_oversized_output_fail_closed(self):
        self.assertIn("unreadable response", self.run_helper("print('not-json')\n"))
        self.assertIn("exceeded", self.run_helper("sys.stdout.write('x' * 1100000)\n"))

    def test_hung_helper_is_bounded(self):
        started = time.monotonic()
        result = self.run_helper("time.sleep(60)\n", command="timeout")
        self.assertIn("timed out", result)
        self.assertLess(time.monotonic() - started, 5)

if __name__ == "__main__":
    unittest.main()
