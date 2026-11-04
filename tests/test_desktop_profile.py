"""Desktop layout and policy discovery never synthesize profile identity."""
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from team_pool.desktop_profile import (DOMAIN, FLAG, DesktopProfileError,
                                      configuration, launcher_enabled, resolve_target)


class DesktopProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.managed = self.root / "managed"
        self.marker = self.home / "Library/Application Support/Claude/config.json"
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text('{"keep":"app state"}')
        self.args = {"uid": os.getuid(), "username": "fixture", "managed_root": self.managed,
                     "system_uid": os.getuid()}

    def resolve(self):
        return resolve_target(self.home, **self.args)

    def write_policy(self, data, *, machine=False):
        path = self.managed / DOMAIN if machine else self.managed / "fixture" / DOMAIN
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(data))
        path.chmod(0o644)
        return path

    def test_standard_target_does_not_write_or_need_config_library(self):
        before = self.marker.read_bytes()
        target = self.resolve()
        self.assertEqual(target.kind, "managed")
        self.assertFalse(launcher_enabled(target, uid=os.getuid(), system_uid=os.getuid()))
        self.assertFalse(self.managed.exists())
        self.assertEqual(self.marker.read_bytes(), before)

    def test_initialized_app_marker_cannot_be_a_link(self):
        outside = self.root / "outside"
        self.marker.rename(outside)
        self.marker.symlink_to(outside)
        with self.assertRaisesRegex(DesktopProfileError, "symlink"):
            self.resolve()

    def test_parent_link_and_unsafe_policy_permissions_fail_closed(self):
        policy = self.write_policy({FLAG: True})
        policy.chmod(0o666)
        with self.assertRaisesRegex(DesktopProfileError, "unsafe"):
            self.resolve()
        policy.chmod(0o644)
        moved = self.managed.with_name("real-managed")
        self.managed.rename(moved)
        self.managed.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(DesktopProfileError, "symlink"):
            self.resolve()

    def test_disabled_machine_or_user_policy_is_not_overridden(self):
        for machine in (True, False):
            with self.subTest(machine=machine):
                path = self.write_policy({FLAG: False}, machine=machine)
                before = path.read_bytes()
                with self.assertRaisesRegex(DesktopProfileError, "administrator"):
                    self.resolve()
                self.assertEqual(path.read_bytes(), before)
                path.unlink()

    def test_existing_managed_settings_require_admin_review(self):
        self.write_policy({"inferenceProvider": "gateway"})
        with self.assertRaisesRegex(DesktopProfileError, "administrator"):
            self.resolve()

    def test_custom_data_directory_is_not_silently_ignored(self):
        with patch.dict(os.environ, {"CLAUDE_USER_DATA_DIR": str(self.root / "other")}):
            with self.assertRaisesRegex(DesktopProfileError, "custom CLAUDE_USER_DATA_DIR"):
                self.resolve()

    def test_wrong_owner_marker_is_rejected(self):
        with self.assertRaisesRegex(DesktopProfileError, "unsafe ownership"):
            configuration(self.marker, owner=os.getuid() + 1)

    def test_ready_managed_target_survives_new_optional_profile(self):
        path = self.write_policy({FLAG: True})
        profiles = self.home / "Library/Application Support/Claude-3p/configLibrary"
        profiles.mkdir(parents=True)
        (profiles / "_meta.json").write_text(json.dumps({"appliedId": "later"}))
        (profiles / "later.json").write_text('{}')
        target = self.resolve()
        self.assertEqual(target.path, path)
        self.assertTrue(launcher_enabled(target, uid=os.getuid(), system_uid=os.getuid()))


if __name__ == "__main__":
    unittest.main()
