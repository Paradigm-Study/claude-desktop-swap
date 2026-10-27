"""Native-store tests use a private temp home and fake Keychain, never live auth."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from team_pool.native import (
    NATIVE_SERVICE, PILOT_SERVICE, NativeDriftError, NativePolicyError,
    NativeKeychainAccessError, NativePayloadError, NativeStore, NativeStoreError,
)

NOW = 1_900_000_000


class FakeKeychain:
    def __init__(self):
        self.items = {}
        self.fail_service = None

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        if self.fail_service == service:
            raise RuntimeError("FAKE_SECRET_FROM_BACKEND_MUST_NOT_ESCAPE")
        self.items[service, account] = value

    def delete_password(self, service, account):
        del self.items[service, account]


def assignment(token="fake-new-access", account="account-b", **overrides):
    value = {
        "account_id": "pool-b", "generation": 1, "state": "fresh",
        "access_token": token, "expires_at": NOW + 3600,
        "scopes": ["user:inference", "user:profile"],
        "renew_after": NOW + 300,
        "account": {"accountUuid": account, "emailAddress": "pilot@example.invalid",
                    "organizationUuid": "org-b", "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x"},
    }
    value.update(overrides)
    return value


class NativeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.backend = FakeKeychain()
        self.store = NativeStore(self.home, self.backend, policy_paths=[],
                                 environ={"USER": "pilot-test"}, clock=lambda: NOW,
                                 lock_timeout=0.02)
        self.old_credentials = {
            "claudeAiOauth": {"accessToken": "fake-old-access", "refreshToken": "fake-old-refresh"},
            "trustedDeviceToken": "fake-old-trusted-device",
            "unknownAccountSecret": "fake-old-account-value",
            "mcpOAuth": {"service": "fake-local-mcp"},
            "pluginSecrets": {"plugin": "fake-local-plugin"},
        }
        self.old_config = {"oauthAccount": {"accountUuid": "account-a", "organizationUuid": "org-a"},
                           "projects": {"/example": {"allowedTools": []}}, "theme": "dark"}
        self.backend.set_password(NATIVE_SERVICE, "pilot-test", json.dumps(self.old_credentials))
        self.store.config.write_text(json.dumps(self.old_config))

    def current_credentials(self):
        return json.loads(self.backend.get_password(NATIVE_SERVICE, "pilot-test"))

    def activate(self):
        return self.store.apply(assignment(), self.store.snapshot()["fingerprint"], initialize=True)

    def test_access_only_with_real_expiry_and_true_scopes(self):
        self.activate()
        credentials = self.current_credentials()
        self.assertEqual(credentials["claudeAiOauth"], {
            "accessToken": "fake-new-access", "expiresAt": (NOW + 3600) * 1000,
            "scopes": ["user:inference", "user:profile"], "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
        })
        self.assertEqual(credentials["mcpOAuth"], self.old_credentials["mcpOAuth"])
        self.assertEqual(credentials["pluginSecrets"], self.old_credentials["pluginSecrets"])
        self.assertNotIn("trustedDeviceToken", credentials)
        self.assertNotIn("unknownAccountSecret", credentials)
        self.assertNotIn("refreshToken", json.dumps(credentials))
        config = json.loads(self.store.config.read_text())
        self.assertEqual(config["projects"], self.old_config["projects"])
        self.assertEqual(config["oauthAccount"]["accountUuid"], "account-b")
        self.assertNotIn("subscriptionType", config["oauthAccount"])
        self.assertEqual(self.store.config.stat().st_mode & 0o777, 0o600)

    def test_backup_only_keychain_and_snapshot_never_returns_secrets(self):
        self.activate()
        backup = json.loads(self.backend.get_password(PILOT_SERVICE, "pilot-test:native-backup"))
        self.assertEqual(backup["credentials"], self.old_credentials)
        self.assertEqual(backup["oauthAccount"], self.old_config["oauthAccount"])
        snapshot = self.store.snapshot()
        self.assertEqual(set(snapshot), {"fingerprint", "has_credentials", "has_backup"})
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn("fake-old-refresh", path.read_text())
                self.assertNotIn("fake-new-access", path.read_text())

    def test_initialize_required_and_snapshot_must_still_match(self):
        expected = self.store.snapshot()["fingerprint"]
        with self.assertRaises(NativeStoreError):
            self.store.apply(assignment(), expected)
        with self.assertRaises(NativeDriftError):
            self.store.apply(assignment(), None, initialize=True)
        with self.assertRaises(NativeDriftError):
            self.store.apply(assignment(), "wrong", initialize=True)
        self.assertIsNone(self.backend.get_password(PILOT_SERVICE, "pilot-test:native-backup"))

    def test_refresh_updates_access_but_never_original_backup(self):
        fingerprint = self.activate()
        original_backup = self.backend.get_password(PILOT_SERVICE, "pilot-test:native-backup")
        refreshed = self.store.apply(assignment(token="fake-next-access", generation=2), fingerprint)
        self.assertNotEqual(refreshed, fingerprint)
        self.assertEqual(self.current_credentials()["claudeAiOauth"]["accessToken"], "fake-next-access")
        self.assertEqual(self.backend.get_password(PILOT_SERVICE, "pilot-test:native-backup"), original_backup)

    def test_plan_is_secret_free_and_matches_both_native_write_stages(self):
        before = self.store.snapshot()["fingerprint"]
        saved_keychain = dict(self.backend.items)
        saved_config = self.store.config.read_bytes()
        plan = self.store.plan(assignment(), before, initialize=True)
        self.assertEqual(set(plan), {"before", "keychain_only", "after"})
        self.assertEqual(plan["before"], before)
        self.assertTrue(all(len(value) == 64 for value in plan.values()))
        self.assertEqual(self.backend.items, saved_keychain)
        self.assertEqual(self.store.config.read_bytes(), saved_config)
        original_writer = self.store._write_config
        intermediate = []

        def observe_config_write(config):
            intermediate.append(self.store.snapshot()["fingerprint"])
            original_writer(config)

        with patch.object(self.store, "_write_config", side_effect=observe_config_write):
            result = self.store.apply(assignment(), before, initialize=True)
        self.assertEqual(intermediate, [plan["keychain_only"]])
        self.assertEqual(result, plan["after"])
        self.assertEqual(self.store.snapshot()["fingerprint"], plan["after"])

    def test_plan_does_not_authorize_overwriting_later_manual_login(self):
        before = self.store.snapshot()["fingerprint"]
        self.store.plan(assignment(), before, initialize=True)
        changed = {"claudeAiOauth": {"accessToken": "fake-manual-login"}}
        self.backend.set_password(NATIVE_SERVICE, "pilot-test", json.dumps(changed))
        with self.assertRaises(NativeDriftError):
            self.store.apply(assignment(), before, initialize=True)
        self.assertEqual(self.current_credentials(), changed)
        self.assertFalse(self.store.snapshot()["has_backup"])

    def test_unchanged_plan_still_validates_backup_policy_and_drift_without_writes(self):
        fingerprint = self.activate()
        saved_keychain = dict(self.backend.items)
        saved_config = self.store.config.read_bytes()
        self.assertEqual(self.store.plan(assignment(), fingerprint),
                         dict.fromkeys(("before", "keychain_only", "after"), fingerprint))
        self.assertEqual(self.backend.items, saved_keychain)
        self.assertEqual(self.store.config.read_bytes(), saved_config)
        with self.assertRaises(NativeDriftError):
            self.store.plan(assignment(), "a" * 64)
        settings = self.store.claude / "settings.json"
        settings.write_text('{"forceLoginOrgUUID":"other-organization"}')
        with self.assertRaises(NativePolicyError):
            self.store.plan(assignment(), fingerprint)
        settings.unlink()
        self.backend.delete_password(PILOT_SERVICE, "pilot-test:native-backup")
        with self.assertRaisesRegex(NativeStoreError, "No enrollment backup"):
            self.store.plan(assignment(), fingerprint)

    def test_complete_native_payload_limit_is_checked_before_any_backup_or_write(self):
        from team_pool.native_security import write_command
        credentials = self.current_credentials()
        credentials["pluginSecrets"] = {"large": "fake-plugin-secret-" * 400}
        self.backend.set_password(NATIVE_SERVICE, "pilot-test", json.dumps(credentials))
        self.backend.validate_password = lambda service, username, value: write_command(username, value)
        before = self.store.snapshot()["fingerprint"]
        for operation in (self.store.plan, self.store.apply):
            with self.assertRaises(NativePayloadError):
                operation(assignment(), before, initialize=True)
            self.assertEqual(self.current_credentials(), credentials)
            self.assertFalse(self.store.snapshot()["has_backup"])
            self.assertEqual(json.loads(self.store.config.read_text()), self.old_config)

    def test_explicit_restore_can_recover_journaled_keychain_only_interruption(self):
        before = self.store.snapshot()["fingerprint"]
        plan = self.store.plan(assignment(), before, initialize=True)
        # BaseException simulates process death: ordinary failure rollback cannot run.
        with patch.object(self.store, "_write_config", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.apply(assignment(), before, initialize=True)
        self.assertEqual(self.store.snapshot()["fingerprint"], plan["keychain_only"])
        self.assertTrue(self.store.snapshot()["has_backup"])
        restored = self.store.restore(plan["keychain_only"])
        self.assertEqual(restored, plan["before"])
        self.assertEqual(self.current_credentials(), self.old_credentials)
        self.assertEqual(json.loads(self.store.config.read_text()), self.old_config)

    def test_display_metadata_changes_do_not_change_account_fingerprint(self):
        fingerprint = self.activate()
        config = json.loads(self.store.config.read_text())
        config["oauthAccount"].update(displayName="Updated name", emailAddress="new@example.invalid",
                                       billingType="organization", hasExtraUsageEnabled=False)
        self.store.config.write_text(json.dumps(config))
        self.assertEqual(self.store.snapshot()["fingerprint"], fingerprint)
        config["oauthAccount"]["organizationUuid"] = "different-organization"
        self.store.config.write_text(json.dumps(config))
        self.assertNotEqual(self.store.snapshot()["fingerprint"], fingerprint)

    def test_manual_login_and_logout_stop_future_writes_and_restore(self):
        fingerprint = self.activate()
        for changed in [{"claudeAiOauth": {"accessToken": "fake-manual-login"}}, {}]:
            self.backend.set_password(NATIVE_SERVICE, "pilot-test", json.dumps(changed))
            with self.assertRaises(NativeDriftError):
                self.store.apply(assignment(token="fake-forbidden-overwrite"), fingerprint)
            with self.assertRaises(NativeDriftError):
                self.store.restore(fingerprint)
            self.assertEqual(self.current_credentials(), changed)

    def test_shared_secret_changes_do_not_trigger_drift_and_survive_restore(self):
        fingerprint = self.activate()
        credentials = self.current_credentials()
        credentials["mcpOAuth"] = {"service": "fake-new-local-mcp"}
        credentials.pop("pluginSecrets")
        self.backend.set_password(NATIVE_SERVICE, "pilot-test", json.dumps(credentials))
        config = json.loads(self.store.config.read_text())
        config["theme"] = "light"
        self.store.config.write_text(json.dumps(config))
        self.assertEqual(self.store.snapshot()["fingerprint"], fingerprint)
        restored = self.store.restore(fingerprint)
        self.assertNotEqual(restored, fingerprint)
        final = self.current_credentials()
        self.assertEqual(final["claudeAiOauth"], self.old_credentials["claudeAiOauth"])
        self.assertEqual(final["trustedDeviceToken"], "fake-old-trusted-device")
        self.assertEqual(final["mcpOAuth"], {"service": "fake-new-local-mcp"})
        self.assertNotIn("pluginSecrets", final)
        self.assertEqual(json.loads(self.store.config.read_text())["theme"], "light")
        self.assertFalse(self.store.snapshot()["has_backup"])

    def test_failed_backup_preserves_native_store_and_sanitizes_error(self):
        self.backend.fail_service = PILOT_SERVICE
        with self.assertRaisesRegex(NativeStoreError, "Keychain write failed") as failure:
            self.activate()
        self.assertNotIn("FAKE_SECRET", str(failure.exception))
        self.assertEqual(self.current_credentials(), self.old_credentials)
        self.assertEqual(json.loads(self.store.config.read_text()), self.old_config)

    def test_keychain_access_denials_remain_typed_but_never_include_backend_text(self):
        from team_pool.macos_keychain import KeychainWriteError
        for status in (-25308, -128, -25293):
            wrapped = RuntimeError("FAKE_SECRET_FROM_BACKEND")
            wrapped.__cause__ = RuntimeError(status, "FAKE_SECRET_FROM_API")
            with self.subTest(status=status, operation="read"), \
                    patch.object(self.backend, "get_password", side_effect=wrapped):
                with self.assertRaises(NativeKeychainAccessError) as raised:
                    self.store.snapshot()
                self.assertEqual(raised.exception.status, status)
                self.assertNotIn("FAKE_SECRET", str(raised.exception))
            with self.subTest(status=status, operation="write"), \
                    patch.object(self.backend, "set_password", side_effect=KeychainWriteError(status)):
                with self.assertRaises(NativeKeychainAccessError) as raised:
                    self.store.set_secret("device-token", "FAKE_SECRET_INPUT")
                self.assertEqual(raised.exception.status, status)
                self.assertNotIn("FAKE_SECRET", str(raised.exception))
            with self.subTest(status=status, operation="delete"), \
                    patch.object(self.backend, "delete_password", side_effect=wrapped):
                with self.assertRaises(NativeKeychainAccessError):
                    self.store._set(NATIVE_SERVICE, "pilot-test", None)

    def test_config_failure_rolls_back_immediate_native_change(self):
        with patch.object(self.store, "_write_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.activate()
        self.assertEqual(self.current_credentials(), self.old_credentials)
        self.assertEqual(json.loads(self.store.config.read_text()), self.old_config)
        self.assertTrue(self.store.snapshot()["has_backup"])

    def test_expired_or_incomplete_assignment_does_not_touch_state(self):
        for bad in [assignment(expires_at=NOW + 30), assignment(expires_at=float("inf")),
                    assignment(expires_at=True), assignment(scopes=[]), assignment(scopes=["user:profile"]), assignment(account={}),
                    assignment(access_token="")]:
            with self.subTest(bad=list(bad)):
                with self.assertRaises(NativeStoreError):
                    self.store.apply(bad, self.store.snapshot()["fingerprint"], initialize=True)
                self.assertEqual(self.current_credentials(), self.old_credentials)
                self.assertFalse(self.store.snapshot()["has_backup"])

    def test_malformed_config_and_symlink_refuse_without_overwrite(self):
        self.store.config.write_text("not json")
        with self.assertRaises(NativeStoreError):
            self.store.snapshot()
        self.store.config.unlink()
        target = self.home / "do-not-touch"
        target.write_text(json.dumps(self.old_config))
        self.store.config.symlink_to(target)
        with self.assertRaises(NativePolicyError):
            self.store.preflight()
        self.assertEqual(json.loads(target.read_text()), self.old_config)

    def test_all_three_native_advisory_locks_exclude_mutations(self):
        self.store.claude.mkdir()
        for path in [self.store.claude / ".oauth_refresh.lock", self.home / ".claude.lock",
                     self.home / ".claude.json.lock"]:
            with self.subTest(lock=path.name):
                path.mkdir()
                # Even an old lock is not silently stolen by the pilot.
                os.utime(path, (1, 1))
                with self.assertRaisesRegex(NativeStoreError, "is busy"):
                    self.activate()
                self.assertTrue(path.is_dir())
                path.rmdir()
                self.assertEqual(self.current_credentials(), self.old_credentials)

    def test_settings_policy_and_required_organization_enforced(self):
        self.store.claude.mkdir()
        settings = self.store.claude / "settings.json"
        cases = [
            {"forceLoginMethod": "console"}, {"forceLoginOrgUUID": "wrong-org"},
            {"allowedProviders": ["anthropic"]}, {"apiKeyHelper": "helper"},
            {"env": {"ANTHROPIC_BASE_URL": "http://example.invalid"}},
            {"forceLoginSomethingNew": True},
        ]
        for case in cases:
            settings.write_text(json.dumps(case))
            with self.assertRaises(NativePolicyError):
                self.store.preflight(assignment())
        settings.write_text(json.dumps({"forceLoginMethod": "claudeai", "forceLoginOrgUUID": "org-b"}))
        self.store.preflight(assignment())

    def test_auth_environment_fallback_file_and_api_key_are_rejected(self):
        self.store.environ["CLAUDE_CONFIG_DIR"] = str(self.home / "other")
        with self.assertRaises(NativePolicyError):
            self.store.preflight()

        del self.store.environ["CLAUDE_CONFIG_DIR"]
        self.store.claude.mkdir()
        fallback = self.store.claude / ".credentials.json"
        fallback.write_text("{}")
        with self.assertRaises(NativePolicyError):
            self.store.preflight()
        fallback.unlink()
        self.backend.set_password("Claude Code", "pilot-test", "fake-api-key")
        with self.assertRaises(NativePolicyError):
            self.store.preflight()

    def test_restore_cannot_bypass_changed_organization_policy(self):
        fingerprint = self.activate()
        (self.store.claude / "settings.json").write_text('{"forceLoginOrgUUID":"org-b"}')
        with self.assertRaises(NativePolicyError):
            self.store.restore(fingerprint)
        self.assertEqual(self.current_credentials()["claudeAiOauth"]["accessToken"], "fake-new-access")
        self.assertTrue(self.store.snapshot()["has_backup"])

    def test_project_policy_checked_when_project_is_supplied(self):
        project = self.home / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude/settings.local.json").write_text('{"forceLoginMethod":"console"}')
        self.store.project_dirs = [project]
        with self.assertRaises(NativePolicyError):
            self.store.preflight(assignment())

    def test_empty_home_can_enroll_and_restore_no_original_login(self):
        self.backend.delete_password(NATIVE_SERVICE, "pilot-test")
        self.store.config.unlink()
        fingerprint = self.activate()
        self.store.restore(fingerprint)
        self.assertIsNone(self.backend.get_password(NATIVE_SERVICE, "pilot-test"))
        self.assertEqual(json.loads(self.store.config.read_text()), {})

    def test_device_secret_is_separate_and_protects_backup_namespace(self):
        self.store.set_secret("device-token", "fake-device-token")
        self.assertEqual(self.store.get_secret("device-token"), "fake-device-token")
        self.store.delete_secret("device-token")
        self.assertIsNone(self.store.get_secret("device-token"))
        with self.assertRaises(NativeStoreError):
            self.store.set_secret("native-backup", "do-not-overwrite")


if __name__ == "__main__":
    unittest.main()
