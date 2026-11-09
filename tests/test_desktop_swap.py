"""Offline tests: all configuration edits are confined to temporary directories."""

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "scripts/desktop_swap.py"
SPEC = importlib.util.spec_from_file_location("desktop_swap", SOURCE)
swap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = swap
SPEC.loader.exec_module(swap)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.layout = swap.Layout(
            self.root / "home", os.getuid(), os.getgid(), self.root / "system",
            system_uid=os.getuid(), system_gid=os.getgid(),
            managed_root=self.root / "Managed Preferences", username="fixture-user",
        )
        self.layout.profiles.mkdir(parents=True)
        self.layout.system_dir.mkdir()
        self.profile = self.layout.profiles / "example-profile.json"
        self.meta = self.layout.profiles / "_meta.json"
        self.save(self.meta, {"appliedId": "example-profile", "other": "unchanged"})
        self.save(self.profile, {"identity": "example", "unrelated": {"setting": 7}})
        self.profile.chmod(0o600)

    @staticmethod
    def save(path, value):
        path.write_text(json.dumps(value))

    @staticmethod
    def load(path):
        return json.loads(path.read_text())

    def test_install_and_uninstall_preserve_unrelated_and_concurrent_settings(self):
        self.save(self.layout.managed, {"unrelated": {"setting": 12}})
        swap.install(self.layout)
        self.assertTrue(self.load(self.profile)[swap.FLAG])
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.profile.stat().st_uid, os.getuid())
        self.assertIsInstance(self.load(self.layout.managed)["processWrapper"], str)
        self.assertEqual(json.loads(self.load(self.layout.managed)["processWrapper"]), [str(self.layout.launcher)])
        self.assertEqual(self.layout.state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("unrelated", self.load(self.layout.state))

        profile = self.load(self.profile)
        profile["later"] = "keep this"
        self.save(self.profile, profile)
        managed = self.load(self.layout.managed)
        managed["later"] = "keep this too"
        self.save(self.layout.managed, managed)
        swap.uninstall(self.layout)
        self.assertEqual(self.load(self.profile), {"identity": "example", "unrelated": {"setting": 7}, "later": "keep this"})
        self.assertEqual(self.load(self.layout.managed), {"unrelated": {"setting": 12}, "later": "keep this too"})
        self.assertFalse(self.layout.launcher.exists())
        self.assertFalse(self.layout.state.exists())

    def test_uninstall_restores_false_flag_and_absent_managed_file(self):
        self.save(self.profile, {swap.FLAG: False})
        swap.install(self.layout)
        swap.uninstall(self.layout)
        self.assertEqual(self.load(self.profile), {swap.FLAG: False})
        self.assertFalse(self.layout.managed.exists())

    def test_uninstall_preserves_preexisting_matching_launcher_and_setting(self):
        launcher = swap.render_launcher(self.layout.uid)
        self.layout.launcher.write_bytes(launcher)
        self.layout.launcher.chmod(0o700)
        self.save(self.layout.managed, {"processWrapper": swap.wrapper_setting(self.layout)})
        self.save(self.profile, {swap.FLAG: True})
        swap.install(self.layout)
        swap.uninstall(self.layout)
        self.assertEqual(self.layout.launcher.read_bytes(), launcher)
        self.assertEqual(self.layout.launcher.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.load(self.layout.managed), {"processWrapper": swap.wrapper_setting(self.layout)})
        self.assertEqual(self.load(self.profile), {swap.FLAG: True})

    def test_install_rejects_conflicting_process_wrapper_without_changes(self):
        self.save(self.layout.managed, {"processWrapper": "another-wrapper"})
        before = self.profile.read_bytes()
        with self.assertRaisesRegex(swap.SetupError, "conflicts"):
            swap.install(self.layout)
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertEqual(self.load(self.layout.managed)["processWrapper"], "another-wrapper")
        self.assertFalse(self.layout.launcher.exists())
        self.assertFalse(self.layout.state.exists())

    def test_install_rejects_conflicting_launcher_without_changes(self):
        self.layout.launcher.write_text("#!/bin/sh\nexit 7\n")
        with self.assertRaisesRegex(swap.SetupError, "conflicts"):
            swap.install(self.layout)
        self.assertFalse(self.layout.managed.exists())
        self.assertNotIn(swap.FLAG, self.load(self.profile))

    def test_install_is_idempotent_but_refuses_different_fast_option(self):
        swap.install(self.layout)
        before = self.layout.state.read_bytes()
        self.assertIn("Already installed", swap.install(self.layout))
        self.assertEqual(self.layout.state.read_bytes(), before)
        with self.assertRaisesRegex(swap.SetupError, "Uninstall first"):
            swap.install(self.layout, restore_fast_control=True)

    def test_reinstall_refuses_a_different_selected_profile(self):
        swap.install(self.layout)
        other_profile = self.layout.profiles / "other-profile.json"
        self.save(other_profile, {"leave": "alone"})
        self.save(self.meta, {"appliedId": "other-profile"})
        before = self.layout.state.read_bytes()
        with self.assertRaisesRegex(swap.SetupError, "different Desktop profile.*Uninstall first"):
            swap.install(self.layout)
        self.assertEqual(self.layout.state.read_bytes(), before)
        self.assertEqual(self.load(other_profile), {"leave": "alone"})

    def test_install_refuses_group_or_world_writable_managed_settings(self):
        self.save(self.layout.managed, {"keep": "unchanged"})
        for mode in (0o664, 0o646):
            with self.subTest(mode=mode):
                self.layout.managed.chmod(mode)
                with self.assertRaisesRegex(swap.SetupError, "unsafe ownership or permissions"):
                    swap.install(self.layout)
                self.assertFalse(self.layout.state.exists())
                self.assertNotIn(swap.FLAG, self.load(self.profile))

    def test_install_rejects_wrong_owner_of_managed_settings_or_profile(self):
        self.save(self.layout.managed, {"keep": "unchanged"})
        real_read = swap.read_file
        for target in (self.layout.managed, self.profile):
            with self.subTest(target=target.name):
                def read_with_wrong_owner(path):
                    snapshot = real_read(path)
                    if path == target:
                        return swap.Snapshot(snapshot.data, snapshot.mode, snapshot.uid + 1, snapshot.gid)
                    return snapshot
                with patch.object(swap, "read_file", side_effect=read_with_wrong_owner):
                    with self.assertRaises(swap.SetupError):
                        swap.install(self.layout)
                self.assertFalse(self.layout.state.exists())
                self.assertNotIn(swap.FLAG, self.load(self.profile))

    def test_status_and_uninstall_reject_unsafe_system_file_ownership(self):
        swap.install(self.layout)
        before = {p: p.read_bytes() for p in (self.layout.managed, self.profile, self.layout.launcher, self.layout.state)}
        real_read = swap.read_file
        for target in (self.layout.managed, self.layout.launcher):
            with self.subTest(target=target.name):
                def read_with_wrong_owner(path):
                    snapshot = real_read(path)
                    if path == target:
                        return swap.Snapshot(snapshot.data, snapshot.mode, snapshot.uid + 1, snapshot.gid)
                    return snapshot
                with patch.object(swap, "read_file", side_effect=read_with_wrong_owner):
                    with self.assertRaisesRegex(swap.SetupError, "unsafe ownership"):
                        swap.status(self.layout)
                    with self.assertRaisesRegex(swap.SetupError, "unsafe ownership"):
                        swap.uninstall(self.layout)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_status_and_uninstall_reject_newly_writable_managed_settings(self):
        swap.install(self.layout)
        self.layout.managed.chmod(0o666)
        with self.assertRaisesRegex(swap.SetupError, "unsafe ownership or permissions"):
            swap.status(self.layout)
        with self.assertRaisesRegex(swap.SetupError, "unsafe ownership or permissions"):
            swap.uninstall(self.layout)
        self.assertTrue(self.layout.state.exists())
        self.assertTrue(self.load(self.profile)[swap.FLAG])

    def test_uninstall_refuses_modified_owned_values_before_any_write(self):
        for target in ("managed", "profile", "launcher"):
            with self.subTest(target=target):
                swap.install(self.layout)
                path = {"managed": self.layout.managed, "profile": self.profile, "launcher": self.layout.launcher}[target]
                original = path.read_bytes()
                if target == "managed":
                    self.save(path, {"processWrapper": "changed"})
                elif target == "profile":
                    self.save(path, {swap.FLAG: False})
                else:
                    path.write_bytes(b"changed")
                snapshots = {p: p.read_bytes() for p in (self.layout.managed, self.layout.launcher, self.profile, self.layout.state)}
                with self.assertRaisesRegex(swap.SetupError, "changed"):
                    swap.uninstall(self.layout)
                self.assertEqual(snapshots, {p: p.read_bytes() for p in snapshots})
                path.write_bytes(original)
                swap.uninstall(self.layout)

    def test_selected_profile_path_traversal_is_rejected(self):
        for profile_id in ("../outside", "/absolute", "a/b", "", None):
            with self.subTest(profile_id=profile_id):
                self.save(self.meta, {"appliedId": profile_id})
                with self.assertRaisesRegex(swap.SetupError, "identifier"):
                    swap.install(self.layout)
                self.assertFalse(self.layout.state.exists())
                self.assertFalse(self.layout.managed.exists())

    def test_leaf_and_parent_symlinks_are_rejected(self):
        outside = self.root / "outside.json"
        outside.write_text('{"untouched": true}')
        self.profile.unlink()
        self.profile.symlink_to(outside)
        with self.assertRaises(swap.SetupError):
            swap.install(self.layout)
        self.assertEqual(outside.read_text(), '{"untouched": true}')
        self.profile.unlink()
        self.save(self.profile, {})
        directory = self.layout.profiles
        moved = directory.with_name("real-configLibrary")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(swap.SetupError):
            swap.install(self.layout)
        self.assertFalse(self.layout.managed.exists())

    def test_failed_write_rolls_back_prior_changes(self):
        original = self.profile.read_bytes()
        real_write = swap.write_file
        def fail_profile(path, data, mode, owner):
            if path == self.profile:
                raise OSError("simulated write failure")
            return real_write(path, data, mode, owner)
        with patch.object(swap, "write_file", side_effect=fail_profile):
            with self.assertRaises(OSError):
                swap.install(self.layout)
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertFalse(self.layout.managed.exists())
        self.assertFalse(self.layout.launcher.exists())
        self.assertFalse(self.layout.state.exists())

    def test_status_warns_if_desktop_profile_changed_and_uninstall_restores_recorded_profile(self):
        swap.install(self.layout)
        other_profile = self.layout.profiles / "other-profile.json"
        self.save(other_profile, {"leave": "alone"})
        self.save(self.meta, {"appliedId": "other-profile"})
        self.assertIn("different Desktop profile", swap.status(self.layout))
        swap.uninstall(self.layout)
        self.assertEqual(self.load(other_profile), {"leave": "alone"})
        self.assertNotIn(swap.FLAG, self.load(self.profile))

    def standard_layout(self):
        self.meta.unlink()
        self.profile.unlink()
        marker = self.layout.home / "Library/Application Support/Claude/config.json"
        marker.parent.mkdir(parents=True)
        self.save(marker, {"locale": "en-US", "appState": "keep"})
        return marker

    def legacy_install(self, **kwargs):
        """Model the a8-a13 writer to test migration and old recovery evidence."""
        target = swap.DesktopTarget("managed", self.layout.managed_root / "fixture-user" / "com.anthropic.claudefordesktop.plist")
        with patch.object(swap, "desktop_target", return_value=target):
            swap.install(self.layout, **kwargs)
        return target

    def test_standard_layout_creates_only_flag_default_library_and_keeps_identity(self):
        marker = self.standard_layout()
        history = marker.parent / "sessions.json"
        history.write_bytes(b"original conversation identity")
        identity = marker.parent / "deploymentMode.json"
        identity.write_bytes(b'{"deploymentMode":"1p"}')
        before = {path: path.read_bytes() for path in (marker, history, identity)}
        swap.install(self.layout)
        target = swap.desktop_target(self.layout)
        self.assertEqual(target.kind, "profile")
        self.assertEqual(self.load(target.path), {swap.FLAG: True})
        self.assertEqual(self.load(self.meta), swap.default_library(target.profile_id))
        self.assertFalse(self.layout.managed_root.exists())
        state = self.load(self.layout.state)
        self.assertEqual(state["schema"], 3)
        self.assertEqual(state["users"][0]["created_library"]["meta"], self.load(self.meta))
        self.assertIn("Already installed", swap.install(self.layout))
        self.assertIn("verified", swap.status(self.layout))
        swap.uninstall(self.layout)
        self.assertFalse(target.path.exists())
        self.assertFalse(self.meta.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_standard_uninstall_preserves_later_profile_settings_and_metadata(self):
        self.standard_layout()
        swap.install(self.layout)
        target = swap.desktop_target(self.layout)
        self.save(target.path, {swap.FLAG: True, "disableNonessentialTelemetry": True})
        meta = self.meta.read_bytes()
        swap.uninstall(self.layout)
        self.assertEqual(self.load(target.path), {"disableNonessentialTelemetry": True})
        self.assertEqual(self.meta.read_bytes(), meta)

    def test_old_owned_cache_migrates_to_existing_selected_profile_only(self):
        self.standard_layout()
        old = self.legacy_install()
        self.save(self.meta, {"appliedId": "later-profile"})
        later = self.layout.profiles / "later-profile.json"
        self.save(later, {"keep": "unchanged"})
        self.assertIn("Migrated", swap.install(self.layout))
        self.assertFalse(old.path.exists())
        self.assertEqual(self.load(later), {"keep": "unchanged", swap.FLAG: True})
        self.assertIn("Already installed", swap.install(self.layout))
        swap.uninstall(self.layout)
        self.assertEqual(self.load(later), {"keep": "unchanged"})

    def test_standard_uninstall_refuses_flag_drift_without_other_writes(self):
        self.standard_layout()
        swap.install(self.layout)
        target = swap.desktop_target(self.layout)
        before = {p: p.read_bytes() for p in (self.layout.managed, self.layout.launcher, self.layout.state)}
        self.save(target.path, {swap.FLAG: False})
        with self.assertRaisesRegex(swap.SetupError, "flag changed"):
            swap.uninstall(self.layout)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_standard_uninstall_refuses_newly_unsafe_library_directory(self):
        self.standard_layout()
        swap.install(self.layout)
        target = swap.desktop_target(self.layout)
        target.path.parent.chmod(0o777)
        with self.assertRaisesRegex(swap.SetupError, "unsafe directory"):
            swap.uninstall(self.layout)
        self.assertTrue(self.layout.state.exists())
        self.assertTrue(target.path.exists())

    def test_standard_failed_write_rolls_back_launcher_library_and_state(self):
        self.standard_layout()
        real_write = swap.write_file
        def fail_state(path, data, mode, owner):
            if path == self.layout.state:
                raise OSError("simulated state write failure")
            return real_write(path, data, mode, owner)
        with patch.object(swap, "write_file", side_effect=fail_state):
            with self.assertRaises(OSError):
                swap.install(self.layout)
        self.assertFalse(self.meta.exists())
        self.assertEqual(list(self.layout.profiles.iterdir()), [])
        self.assertFalse(self.layout.managed.exists())
        self.assertFalse(self.layout.launcher.exists())
        self.assertFalse(self.layout.state.exists())
        self.assertFalse((self.layout.system_dir / swap.TRANSACTION_NAME).exists())

    def test_missing_standard_data_is_actionable_before_any_writes(self):
        self.meta.unlink()
        with self.assertRaisesRegex(swap.SetupError, "first sign-in.*no profile needs to be created"):
            swap.install(self.layout)
        self.assertFalse(self.layout.managed.exists())
        self.assertFalse(self.layout.launcher.exists())
        self.assertFalse(self.layout.state.exists())

    def test_profile_installs_retain_compatible_schema_one(self):
        swap.install(self.layout)
        state = self.load(self.layout.state)
        self.assertEqual(state["schema"], 1)
        self.assertNotIn("target_kind", state)
        self.assertNotIn("policy_existed", state)

    def test_created_directories_ignore_private_umask_and_files_stay_private(self):
        self.standard_layout()
        self.layout.system_dir.rmdir()
        self.layout.profiles.rmdir()
        self.layout.profiles.parent.rmdir()
        previous_umask = os.umask(0o077)
        try:
            swap.make_system_directory(self.layout)
            with swap.installation_lock(self.layout):
                swap.install(self.layout)
        finally:
            os.umask(previous_umask)
        target = swap.desktop_target(self.layout)
        self.assertEqual(self.layout.system_dir.stat().st_mode & 0o777, 0o755)
        self.assertEqual(target.path.parent.stat().st_mode & 0o777, 0o700)
        for path in (self.layout.state, self.layout.system_dir / swap.LOCK_NAME):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.layout.managed.stat().st_mode & 0o777, 0o644)
        self.assertEqual(target.path.stat().st_mode & 0o777, 0o600)

    def test_existing_safe_directories_are_not_automatically_widened(self):
        self.standard_layout()
        self.layout.system_dir.chmod(0o700)
        self.layout.managed_root.mkdir(mode=0o700)
        user_directory = self.layout.managed_root / "fixture-user"
        user_directory.mkdir(mode=0o700)
        swap.make_system_directory(self.layout)
        swap.make_policy_directory(self.layout, swap.desktop_target(self.layout))
        for path in (self.layout.system_dir, self.layout.managed_root, user_directory):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_a7_lock_only_leftover_repair_then_standard_install(self):
        marker = self.standard_layout()
        before = marker.read_bytes()
        self.layout.system_dir.chmod(0o700)
        lock = self.layout.system_dir / swap.LOCK_NAME
        lock.write_bytes(b"501")
        lock.chmod(0o600)
        self.assertIn("Repaired", swap.repair_access(self.layout))
        self.assertEqual(self.layout.system_dir.stat().st_mode & 0o777, 0o755)
        self.assertEqual(lock.read_bytes(), b"501")
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        with swap.installation_lock(self.layout):
            swap.install(self.layout)
        self.assertTrue(self.layout.state.exists())
        self.assertEqual(self.layout.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(marker.read_bytes(), before)
        self.assertTrue(self.meta.exists())
        self.assertIn("already readable", swap.repair_access(self.layout))

    def test_empty_leftover_repair_creates_private_lock(self):
        self.layout.system_dir.chmod(0o700)
        self.assertIn("Repaired", swap.repair_access(self.layout))
        self.assertEqual((self.layout.system_dir / swap.LOCK_NAME).stat().st_mode & 0o777, 0o600)
        self.assertIn("already readable", swap.repair_access(self.layout))

    def test_repair_fixes_group_denial_even_when_other_users_can_read(self):
        self.layout.system_dir.chmod(0o705)
        self.assertIn("Repaired", swap.repair_access(self.layout))
        self.assertEqual(self.layout.system_dir.stat().st_mode & 0o777, 0o755)

    def test_intact_owned_install_repairs_only_public_directory_modes(self):
        self.standard_layout()
        with swap.installation_lock(self.layout):
            self.legacy_install()
        target = swap.desktop_target(self.layout)
        directories = (self.layout.system_dir, self.layout.managed_root, target.path.parent)
        files = (self.layout.state, self.layout.managed, self.layout.launcher,
                 self.layout.system_dir / swap.LOCK_NAME, target.path)
        before = {path: (path.read_bytes(), path.stat().st_mode) for path in files}
        for path in directories:
            path.chmod(0o700)
        self.assertIn("Repaired", swap.repair_access(self.layout))
        for path in directories:
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mode) for path in files})

    def test_repair_does_not_widen_shared_policy_root_with_other_contents(self):
        self.standard_layout()
        with swap.installation_lock(self.layout):
            self.legacy_install()
        target = swap.desktop_target(self.layout)
        (self.layout.managed_root / "another-user").mkdir()
        directories = (self.layout.system_dir, self.layout.managed_root, target.path.parent)
        for path in directories:
            path.chmod(0o700)
        with self.assertRaisesRegex(swap.SetupError, "nothing was widened"):
            swap.repair_access(self.layout)
        for path in directories:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_repair_rejects_unknown_files_before_any_widening(self):
        self.layout.system_dir.chmod(0o700)
        unknown = self.layout.system_dir / "unrelated.json"
        unknown.write_text('{"preserve":true}')
        with self.assertRaisesRegex(swap.SetupError, "nothing was widened"):
            swap.repair_access(self.layout)
        self.assertEqual(self.layout.system_dir.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.layout.system_dir / swap.LOCK_NAME).exists())
        self.assertEqual(unknown.read_text(), '{"preserve":true}')

    def test_repair_rejects_unsafe_directory_and_lock_modes(self):
        lock = self.layout.system_dir / swap.LOCK_NAME
        lock.write_text("keep")
        for directory_mode, lock_mode in ((0o777, 0o600), (0o700, 0o644)):
            with self.subTest(directory_mode=directory_mode, lock_mode=lock_mode):
                self.layout.system_dir.chmod(directory_mode)
                lock.chmod(lock_mode)
                with self.assertRaisesRegex(swap.SetupError, "nothing was widened"):
                    swap.repair_access(self.layout)
                self.assertEqual(self.layout.system_dir.stat().st_mode & 0o777, directory_mode)
                self.assertEqual(lock.stat().st_mode & 0o777, lock_mode)
                self.assertEqual(lock.read_text(), "keep")

    def test_repair_rejects_directory_and_lock_symlinks(self):
        outside = self.root / "outside-lock"
        outside.write_bytes(b"keep")
        lock = self.layout.system_dir / swap.LOCK_NAME
        lock.symlink_to(outside)
        with self.assertRaises(OSError):
            swap.repair_access(self.layout)
        lock.unlink()
        moved = self.root / "actual-system"
        self.layout.system_dir.rename(moved)
        self.layout.system_dir.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            swap.repair_access(self.layout)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_repair_rejects_other_directory_owner(self):
        other = swap.Layout(self.layout.home, self.layout.uid, self.layout.gid,
                            self.layout.system_dir, system_uid=os.getuid() + 1)
        self.layout.system_dir.chmod(0o700)
        with self.assertRaisesRegex(swap.SetupError, "nothing was widened"):
            swap.repair_access(other)
        self.assertEqual(self.layout.system_dir.stat().st_mode & 0o777, 0o700)

    def test_repair_waits_for_existing_installer_lock(self):
        self.layout.system_dir.chmod(0o700)
        with swap.installation_lock(self.layout):
            with self.assertRaisesRegex(swap.SetupError, "Another installer"):
                swap.repair_access(self.layout)
        self.assertEqual(self.layout.system_dir.stat().st_mode & 0o777, 0o700)

    def test_invalid_metadata_does_not_fall_back_to_managed_policy(self):
        self.save(self.meta, {"appliedId": "../other"})
        marker = self.layout.home / "Library/Application Support/Claude/config.json"
        marker.parent.mkdir(parents=True)
        self.save(marker, {})
        with self.assertRaisesRegex(swap.SetupError, "selected profile identifier is invalid"):
            swap.install(self.layout)
        self.assertFalse(self.layout.managed_root.exists())


class MultiUserInstallationTests(unittest.TestCase):
    setUp = InstallationTests.setUp
    save = staticmethod(InstallationTests.save)
    load = staticmethod(InstallationTests.load)
    standard_layout = InstallationTests.standard_layout
    legacy_install = InstallationTests.legacy_install

    def second_user(self):
        other = swap.Layout(
            self.root / "second-home", self.layout.uid + 10000, self.layout.gid,
            self.layout.system_dir, self.layout.system_uid, self.layout.system_gid,
            self.layout.managed_root, "second-user",
        )
        other.home.mkdir()
        accounts = {layout.uid: SimpleNamespace(pw_dir=str(layout.home), pw_gid=layout.gid,
                                                pw_name=layout.username)
                    for layout in (self.layout, other)}
        lookup = patch.object(swap.pwd, "getpwuid", side_effect=accounts.__getitem__)
        lookup.start()
        self.addCleanup(lookup.stop)
        target = swap.DesktopTarget("managed", other.managed_root / other.username / "com.anthropic.claudefordesktop.plist")
        # Tests cannot chown a second user's Desktop initialization marker. Only
        # discovery is stubbed; real state, policy, launcher and rollback I/O run.
        real_target = swap.desktop_target
        discovery = patch.object(swap, "desktop_target", side_effect=lambda layout, **kwargs: target if layout == other else real_target(layout, **kwargs))
        discovery.start()
        self.addCleanup(discovery.stop)
        return other, target

    def snapshots(self):
        return {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_uid)
                for path in self.root.rglob("*") if path.is_file()}

    def test_schema_two_migration_retains_first_policy_and_machine_originals(self):
        self.standard_layout()
        self.save(self.layout.managed, {"unrelated": 7})
        self.legacy_install()
        first_policy = swap.desktop_target(self.layout).path
        first_bytes = first_policy.read_bytes()
        managed_bytes = self.layout.managed.read_bytes()
        original = self.load(self.layout.state)
        second, second_target = self.second_user()
        swap.install(second, restore_fast_control=True)
        state = self.load(self.layout.state)
        self.assertEqual(state["schema"], 3)
        self.assertEqual(state["managed_original"], original["managed_original"])
        self.assertEqual(state["managed_existed"], original["managed_existed"])
        self.assertEqual(state["users"][0]["profile_original"], original["profile_original"])
        self.assertEqual(first_policy.read_bytes(), first_bytes)
        self.assertEqual(self.layout.managed.read_bytes(), managed_bytes)
        self.assertEqual(plistlib.loads(second_target.path.read_bytes()), {swap.FLAG: True})
        launcher = self.layout.launcher.read_text()
        for uid in (self.layout.uid, second.uid):
            self.assertIn(f'if [ "$(/usr/bin/id -u)" = "{uid}" ]; then', launcher)
        self.assertEqual(launcher.count("unset CLAUDE_CODE_DISABLE_FAST_MODE"), 1)
        self.assertEqual(launcher.count('exec "$@"'), 1)
        before = self.snapshots()
        self.assertIn("verified", swap.status(self.layout))
        self.assertIn("verified", swap.status(second))
        self.assertEqual(self.snapshots(), before)
        self.assertIn("verified", swap.status(self.layout))
        self.assertIn("verified", swap.status(second))

    def test_schema_one_migration_preserves_profile_and_restores_each_user(self):
        self.save(self.profile, {swap.FLAG: False, "keep": "profile"})
        swap.install(self.layout)
        second, second_target = self.second_user()
        first_bytes = self.profile.read_bytes()
        swap.install(second)
        self.assertEqual(self.profile.read_bytes(), first_bytes)
        self.assertEqual(self.load(self.layout.state)["users"][0]["target_kind"], "profile")
        swap.uninstall(second)
        self.assertFalse(second_target.path.exists())
        self.assertEqual(self.profile.read_bytes(), first_bytes)
        self.assertIn("verified", swap.status(self.layout))
        swap.uninstall(self.layout)
        self.assertEqual(self.load(self.profile), {swap.FLAG: False, "keep": "profile"})
        self.assertFalse(self.layout.launcher.exists())
        self.assertFalse(self.layout.managed.exists())
        self.assertFalse(self.layout.state.exists())

    def test_first_user_removal_retains_second_and_final_removal_restores_original_launcher(self):
        self.standard_layout()
        original_launcher = swap.render_launcher(self.layout.uid)
        self.layout.launcher.write_bytes(original_launcher)
        self.layout.launcher.chmod(0o700)
        self.save(self.layout.managed, {"processWrapper": swap.wrapper_setting(self.layout), "keep": 9})
        self.legacy_install()
        first_target = swap.desktop_target(self.layout)
        second, second_target = self.second_user()
        swap.install(second, restore_fast_control=True)
        second_bytes = second_target.path.read_bytes()
        managed_bytes = self.layout.managed.read_bytes()
        swap.uninstall(self.layout)
        self.assertFalse(first_target.path.exists())
        self.assertEqual(second_target.path.read_bytes(), second_bytes)
        self.assertEqual(self.layout.managed.read_bytes(), managed_bytes)
        self.assertEqual(self.layout.launcher.read_bytes(), swap.render_launcher(second.uid, True))
        self.assertIn("Not installed for this", swap.status(self.layout))
        self.assertIn("verified", swap.status(second))
        with self.assertRaisesRegex(swap.SetupError, "not installed"):
            swap.uninstall(self.layout)
        # Changes unrelated to the owned flag survive final restoration.
        second_target.path.write_bytes(plistlib.dumps({swap.FLAG: True, "keep": "later"}))
        swap.uninstall(second)
        self.assertEqual(plistlib.loads(second_target.path.read_bytes()), {"keep": "later"})
        self.assertEqual(self.layout.managed.read_bytes(), managed_bytes)
        self.assertEqual(self.layout.launcher.read_bytes(), original_launcher)
        self.assertEqual(self.layout.launcher.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.layout.state.exists())

    def test_missing_other_user_legacy_cache_does_not_block_new_user(self):
        self.standard_layout()
        self.legacy_install()
        old = swap.desktop_target(self.layout).path
        old.unlink()
        original = self.load(self.layout.state)
        second, _ = self.second_user()
        swap.install(second)
        state = self.load(self.layout.state)
        first = next(user for user in state["users"] if user["uid"] == self.layout.uid)
        self.assertEqual(first["profile_original"], original["profile_original"])
        self.assertEqual(first["target_kind"], "managed")
        self.assertFalse(old.exists())
        self.assertIn("verified", swap.status(second))

    def test_prior_user_drift_and_unsafe_state_refuse_migration(self):
        self.standard_layout()
        self.legacy_install()
        second, _ = self.second_user()
        first = swap.desktop_target(self.layout).path
        for path, replacement, mode in (
            (first, plistlib.dumps({swap.FLAG: False}), None),
            (self.layout.managed, b'{"processWrapper":"third-party"}', None),
            (self.layout.launcher, b"#!/bin/sh\nexit 7\n", None),
            (self.layout.state, None, 0o644),
        ):
            with self.subTest(path=path):
                original = path.read_bytes(), path.stat().st_mode & 0o777
                if replacement is not None:
                    path.write_bytes(replacement)
                if mode is not None:
                    path.chmod(mode)
                before = self.snapshots()
                with self.assertRaises(swap.SetupError):
                    swap.install(second)
                self.assertEqual(self.snapshots(), before)
                path.write_bytes(original[0])
                path.chmod(original[1])

    def test_shared_state_rejects_duplicate_or_reassigned_identity_without_writes(self):
        self.standard_layout()
        self.legacy_install()
        second, _ = self.second_user()
        swap.install(second)
        original = self.layout.state.read_bytes()
        for alteration in ("duplicate", "home", "username", "gid"):
            with self.subTest(alteration=alteration):
                state = json.loads(original)
                if alteration == "duplicate":
                    state["users"].append(dict(state["users"][0]))
                else:
                    state["users"][0][alteration] = 12345 if alteration == "gid" else "/different"
                self.save(self.layout.state, state)
                before = self.snapshots()
                with self.assertRaises(swap.SetupError):
                    swap.uninstall(second)
                self.assertEqual(self.snapshots(), before)
        self.layout.state.write_bytes(original)

    def test_second_install_failure_rolls_back_to_exact_legacy_installation(self):
        self.standard_layout()
        self.legacy_install()
        second, _ = self.second_user()
        before = self.snapshots()
        real_write = swap.write_file
        def fail_state(path, data, mode, owner):
            if path == self.layout.state:
                raise OSError("simulated failure")
            return real_write(path, data, mode, owner)
        with patch.object(swap, "write_file", side_effect=fail_state):
            with self.assertRaises(OSError):
                swap.install(second)
        self.assertEqual(self.snapshots(), before)
        self.assertIn("verified", swap.status(self.layout))

    def test_second_install_checks_other_user_policy_again_before_writing(self):
        self.standard_layout()
        self.legacy_install()
        first = swap.desktop_target(self.layout).path
        second, _ = self.second_user()
        before_launcher = self.layout.launcher.read_bytes()
        before_state = self.layout.state.read_bytes()
        real_make = swap.make_policy_directory
        def drift(layout, target):
            real_make(layout, target)
            first.write_bytes(plistlib.dumps({swap.FLAG: False}))
        with patch.object(swap, "make_policy_directory", side_effect=drift):
            with self.assertRaisesRegex(swap.SetupError, "changed during setup"):
                swap.install(second)
        self.assertEqual(self.layout.launcher.read_bytes(), before_launcher)
        self.assertEqual(self.layout.state.read_bytes(), before_state)

    def test_prior_policy_symlink_and_wrong_owner_are_rejected(self):
        self.standard_layout()
        self.legacy_install()
        first = swap.desktop_target(self.layout).path
        second, _ = self.second_user()
        real_read = swap.read_file
        def wrong_owner(path):
            snapshot = real_read(path)
            if path == first:
                return swap.Snapshot(snapshot.data, snapshot.mode, snapshot.uid + 1, snapshot.gid)
            return snapshot
        before = self.snapshots()
        with patch.object(swap, "read_file", side_effect=wrong_owner):
            with self.assertRaisesRegex(swap.SetupError, "unsafe ownership"):
                swap.install(second)
        self.assertEqual(self.snapshots(), before)
        moved = first.with_name("original-policy")
        first.rename(moved)
        first.symlink_to(moved)
        with self.assertRaises(swap.SetupError):
            swap.install(second)
        self.assertEqual(self.layout.state.read_bytes(), before[self.layout.state][0])

    def test_shared_launcher_passes_existing_client_verification_for_each_user(self):
        from team_pool import client, desktop_profile
        self.standard_layout()
        self.legacy_install()
        first_target = swap.desktop_target(self.layout)
        second, second_target = self.second_user()
        swap.install(second)
        for layout, target in ((self.layout, first_target), (second, second_target)):
            with self.subTest(uid=layout.uid):
                with patch.object(desktop_profile, "resolve_target", return_value=target), patch.object(os, "getuid", return_value=layout.uid):
                    client.verify_desktop_wrapper(home=layout.home, system_dir=layout.system_dir,
                                                  managed_root=layout.managed_root, system_uid=layout.system_uid)

    def test_access_repair_validates_both_users_without_changing_files(self):
        self.standard_layout()
        with swap.installation_lock(self.layout):
            swap.install(self.layout)
        second, _ = self.second_user()
        swap.install(second)
        before = self.snapshots()
        self.assertIn("already readable", swap.repair_access(second))
        self.assertEqual(self.snapshots(), before)



class DurableNativeConfigurationTests(unittest.TestCase):
    setUp = InstallationTests.setUp
    save = staticmethod(InstallationTests.save)
    load = staticmethod(InstallationTests.load)
    standard_layout = InstallationTests.standard_layout
    legacy_install = InstallationTests.legacy_install
    second_user = MultiUserInstallationTests.second_user
    snapshots = MultiUserInstallationTests.snapshots

    def test_absent_owned_cache_migrates_and_cache_deletion_cannot_break_readiness(self):
        from team_pool.desktop_profile import launcher_enabled
        marker = self.standard_layout()
        before = marker.read_bytes()
        old = self.legacy_install()
        original = self.load(self.layout.state)
        old.path.unlink()  # The OS refreshes Managed Preferences; it is a cache.
        self.assertIn("Migrated", swap.install(self.layout))
        target = swap.desktop_target(self.layout)
        self.assertEqual(target.kind, "profile")
        self.assertTrue(launcher_enabled(target, uid=self.layout.uid))
        self.assertFalse(old.path.exists())
        self.assertEqual(marker.read_bytes(), before)
        state = self.load(self.layout.state)
        for key in ("managed_original", "managed_existed", "launcher_existed"):
            self.assertEqual(state[key], original[key])
        self.assertIn("Already installed", swap.install(self.layout))
        swap.uninstall(self.layout)
        self.assertFalse(old.path.exists())
        self.assertFalse(self.meta.exists())

    def test_legacy_cache_is_removed_only_after_native_metadata_and_state_commit(self):
        self.standard_layout()
        old = self.legacy_install()
        writes = []
        real_write = swap.write_file
        def tracked(path, data, mode, owner):
            writes.append((path, data is None))
            return real_write(path, data, mode, owner)
        with patch.object(swap, "write_file", side_effect=tracked):
            swap.install(self.layout)
        self.assertLess(writes.index((self.meta, False)), writes.index((self.layout.state, False)))
        self.assertLess(writes.index((self.layout.state, False)), writes.index((old.path, True)))

    def test_migration_changes_only_current_user_and_retains_other_legacy_record(self):
        self.standard_layout()
        first = self.legacy_install()
        second, target = self.second_user()
        swap.install(second)
        original = self.load(self.layout.state)
        other = next(user for user in original["users"] if user["uid"] == second.uid)
        other_bytes = target.path.read_bytes()
        launcher = self.layout.launcher.read_bytes()
        self.assertIn("Migrated", swap.install(self.layout))
        state = self.load(self.layout.state)
        self.assertEqual(next(user for user in state["users"] if user["uid"] == second.uid), other)
        self.assertEqual(target.path.read_bytes(), other_bytes)
        self.assertEqual(self.layout.launcher.read_bytes(), launcher)
        self.assertFalse(first.path.exists())
        self.assertIn("verified", swap.status(self.layout))

    def test_current_migration_tolerates_other_users_missing_generated_cache(self):
        self.standard_layout()
        self.legacy_install()
        second, target = self.second_user()
        swap.install(second)
        other = next(user for user in self.load(self.layout.state)["users"] if user["uid"] == second.uid)
        target.path.unlink()
        self.assertIn("Migrated", swap.install(self.layout))
        state = self.load(self.layout.state)
        self.assertEqual(next(user for user in state["users"] if user["uid"] == second.uid), other)
        self.assertFalse(target.path.exists())
        self.assertIn("verified", swap.status(self.layout))

    def test_migration_refuses_false_policy_and_keeps_all_files(self):
        self.standard_layout()
        old = self.legacy_install()
        old.path.write_bytes(plistlib.dumps({swap.FLAG: False}))
        before = self.snapshots()
        with self.assertRaisesRegex(swap.SetupError, "flag changed"):
            swap.install(self.layout)
        self.assertEqual(self.snapshots(), before)

    def test_uninstall_of_missing_legacy_cache_does_not_recreate_os_cache(self):
        self.standard_layout()
        old = self.legacy_install()
        old.path.unlink()
        old.path.parent.rmdir()
        swap.uninstall(self.layout)
        self.assertFalse(old.path.exists())
        self.assertFalse(self.layout.state.exists())
        self.assertFalse(self.meta.exists())

    def test_migration_does_not_delete_or_override_unrelated_mdm(self):
        self.standard_layout()
        old = self.legacy_install()
        old.path.write_bytes(plistlib.dumps({swap.FLAG: True, "companyPolicy": "keep"}))
        before = self.snapshots()
        with self.assertRaisesRegex(swap.SetupError, "administrator"):
            swap.install(self.layout)
        self.assertEqual(self.snapshots(), before)

    def test_preexisting_true_policy_is_retained_through_migration_and_uninstall(self):
        self.standard_layout()
        policy = self.layout.managed_root / "fixture-user" / "com.anthropic.claudefordesktop.plist"
        policy.parent.mkdir(parents=True)
        policy.write_bytes(plistlib.dumps({swap.FLAG: True}))
        original = policy.read_bytes()
        self.legacy_install()
        self.assertIn("Migrated", swap.install(self.layout))
        self.assertEqual(policy.read_bytes(), original)
        self.assertIn("Already installed", swap.install(self.layout))
        swap.uninstall(self.layout)
        self.assertEqual(policy.read_bytes(), original)

    def test_uninstall_does_not_claim_new_mdm_cache_after_removing_old_owned_cache(self):
        self.standard_layout()
        old = self.legacy_install()
        swap.install(self.layout)
        self.assertFalse(old.path.exists())
        new_policy = plistlib.dumps({swap.FLAG: True, "newAdministratorSetting": "keep"})
        old.path.write_bytes(new_policy)
        swap.uninstall(self.layout)
        self.assertEqual(old.path.read_bytes(), new_policy)

    def test_nonempty_orphan_library_is_never_adopted(self):
        self.standard_layout()
        orphan = self.layout.profiles / "unselected.json"
        orphan.write_text('{"inferenceProvider":"do not adopt"}')
        before = self.snapshots()
        with self.assertRaisesRegex(swap.SetupError, "files but no metadata"):
            swap.install(self.layout)
        self.assertEqual(self.snapshots(), before)

    def test_new_library_refuses_symlink_or_unsafe_directory(self):
        self.standard_layout()
        self.layout.profiles.chmod(0o777)
        with self.assertRaisesRegex(swap.SetupError, "unsafe directory"):
            swap.install(self.layout)
        self.layout.profiles.chmod(0o700)
        moved = self.layout.profiles.with_name("actual-library")
        self.layout.profiles.rename(moved)
        self.layout.profiles.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(swap.SetupError, "symlink"):
            swap.install(self.layout)
        self.assertFalse(self.layout.state.exists())

    def interrupted_migration(self, phase):
        self.standard_layout()
        old = self.legacy_install()
        real_write = swap.write_file
        def interrupted(path, data, mode, owner):
            real_write(path, data, mode, owner)
            hit = ((phase == "profile" and path.parent == self.layout.profiles and path != self.meta)
                   or (phase == "meta" and path == self.meta)
                   or (phase == "state" and path == self.layout.state)
                   or (phase == "cache" and path == old.path and data is None))
            if hit:
                raise KeyboardInterrupt("simulated process termination")
        with patch.object(swap, "write_file", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                swap.install(self.layout)
        self.assertTrue((self.layout.system_dir / swap.TRANSACTION_NAME).exists())
        if phase != "cache":
            self.assertTrue(old.path.exists())
        self.assertIn("Already installed", swap.install(self.layout))
        self.assertFalse((self.layout.system_dir / swap.TRANSACTION_NAME).exists())
        self.assertFalse(old.path.exists())
        self.assertIn("verified", swap.status(self.layout))
        self.assertEqual(self.load(swap.desktop_target(self.layout).path), {swap.FLAG: True})

    def test_migration_recovers_crash_after_profile(self):
        self.interrupted_migration("profile")

    def test_migration_recovers_crash_after_metadata(self):
        self.interrupted_migration("meta")

    def test_migration_recovers_crash_after_state(self):
        self.interrupted_migration("state")

    def test_migration_recovers_crash_after_cache_removal(self):
        self.interrupted_migration("cache")

    def test_fresh_install_recovers_interruption_without_orphan_adoption(self):
        self.standard_layout()
        real_write = swap.write_file
        def interrupted(path, data, mode, owner):
            real_write(path, data, mode, owner)
            if path.parent == self.layout.profiles and path != self.meta:
                raise KeyboardInterrupt()
        with patch.object(swap, "write_file", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                swap.install(self.layout)
        self.assertFalse(self.meta.exists())
        self.assertIn("Already installed", swap.install(self.layout))
        self.assertIn("verified", swap.status(self.layout))

    def test_large_profile_journal_is_recognized_and_recoverable(self):
        from team_pool.setup import launcher_transaction_pending
        original = {"unrelated": "x" * (400 * 1024)}
        self.save(self.profile, original)
        real_write = swap.write_file
        def interrupted(path, data, mode, owner):
            real_write(path, data, mode, owner)
            if path == self.profile:
                raise KeyboardInterrupt()
        with patch.object(swap, "write_file", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                swap.install(self.layout)
        journal = self.layout.system_dir / swap.TRANSACTION_NAME
        self.assertGreater(journal.stat().st_size, 1024 * 1024)
        self.assertTrue(launcher_transaction_pending(system_dir=self.layout.system_dir,
                                                     system_uid=self.layout.system_uid))
        self.assertIn("Already installed", swap.install(self.layout))
        self.assertEqual(self.load(self.profile), dict(original, **{swap.FLAG: True}))
        self.assertFalse(journal.exists())

    def test_oversized_serialized_journal_is_rejected_before_any_write(self):
        self.save(self.layout.managed, {"unrelated": "x" * (7 * 1024 * 1024)})
        before = self.snapshots()
        with patch.object(swap, "write_file", wraps=swap.write_file) as write:
            with self.assertRaisesRegex(swap.SetupError, "journal would exceed"):
                swap.install(self.layout)
        write.assert_not_called()
        self.assertEqual(self.snapshots(), before)
        self.assertFalse((self.layout.system_dir / swap.TRANSACTION_NAME).exists())

    def test_recovery_preserves_intervening_user_edit_and_journal(self):
        self.standard_layout()
        real_write = swap.write_file
        def interrupted(path, data, mode, owner):
            real_write(path, data, mode, owner)
            if path == self.meta:
                raise KeyboardInterrupt()
        with patch.object(swap, "write_file", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                swap.install(self.layout)
        self.save(self.meta, {"appliedId": "user-edit"})
        before = self.snapshots()
        with self.assertRaisesRegex(swap.SetupError, "changed after interrupted"):
            swap.install(self.layout)
        self.assertEqual(self.snapshots(), before)

    def test_uninstall_recovers_interruption_and_preserves_later_metadata(self):
        self.standard_layout()
        swap.install(self.layout)
        target = swap.desktop_target(self.layout)
        real_write = swap.write_file
        def interrupted(path, data, mode, owner):
            real_write(path, data, mode, owner)
            if path == target.path:
                raise KeyboardInterrupt()
        with patch.object(swap, "write_file", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                swap.uninstall(self.layout)
        self.assertIn("Uninstalled", swap.uninstall(self.layout))
        self.assertFalse(self.layout.state.exists())
        swap.install(self.layout)
        target = swap.desktop_target(self.layout)
        meta = self.load(self.meta)
        meta["entries"].append({"id": "added", "name": "Other"})
        self.save(self.meta, meta)
        swap.uninstall(self.layout)
        self.assertEqual(self.load(self.meta), meta)
        self.assertEqual(self.load(target.path), {})

    def test_runtime_upgrade_preserves_originals_and_other_users(self):
        runtime = str(self.layout.home / ".local/share/claude-desktop-swap/releases/v0.1.0a14/venv/bin/python")
        newer = runtime.replace("a14", "a15")
        self.save(self.profile, {swap.FLAG: False, "keep": 1})
        swap.install(self.layout, gui_runtime=runtime)
        original = self.load(self.layout.state)
        second, _ = self.second_user()
        swap.install(second)
        other = next(record for record in self.load(self.layout.state)["users"] if record["uid"] == second.uid)
        self.assertIn("Updated", swap.install(self.layout, gui_runtime=newer))
        state = self.load(self.layout.state)
        own = next(record for record in state["users"] if record["uid"] == self.layout.uid)
        self.assertEqual(own["gui_runtime"], newer)
        self.assertEqual(own["profile_original"], original["profile_original"])
        self.assertEqual(next(record for record in state["users"] if record["uid"] == second.uid), other)
        self.assertEqual(state["managed_original"], original["managed_original"])
        self.assertIn("Already installed", swap.install(self.layout, gui_runtime=newer))
        swap.uninstall(self.layout)
        self.assertEqual(self.load(self.profile), {swap.FLAG: False, "keep": 1})
        self.assertNotIn(newer, self.layout.launcher.read_text())

    def test_runtime_validation_does_not_allow_other_user_or_arbitrary_program(self):
        before = self.snapshots()
        for runtime in ("/bin/sh", str(self.root / "other/.local/share/claude-desktop-swap/releases/v1/venv/bin/python"),
                        str(self.layout.home / ".local/share/claude-desktop-swap/releases/../v1/venv/bin/python")):
            with self.subTest(runtime=runtime), self.assertRaises(swap.SetupError):
                swap.install(self.layout, gui_runtime=runtime)
        self.assertEqual(self.snapshots(), before)

    def test_gui_runtime_launcher_remains_readable_by_client_verification(self):
        from team_pool import client, desktop_profile
        runtime = str(self.layout.home / ".local/share/claude-desktop-swap/releases/v0.1.0a14/venv/bin/python")
        swap.install(self.layout, gui_runtime=runtime)
        target = swap.desktop_target(self.layout)
        with patch.object(desktop_profile, "resolve_target", return_value=target):
            client.verify_desktop_wrapper(home=self.layout.home, system_dir=self.layout.system_dir,
                                          managed_root=self.layout.managed_root, system_uid=self.layout.system_uid,
                                          gui_runtime=runtime)


class LauncherTests(unittest.TestCase):
    def run_gui_launcher(self, *, target_uid=None, own_remote=True, runtime_enabled=True):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home with 'quotes' $(literal)"
            runtime = home / ".local/share/claude-desktop-swap/releases/v0.1.0a14/venv/bin/python"
            remote = home / ".claude/remote/ccd-cli/2.1.317-example"
            other = Path(directory).resolve() / "another-user/.claude/remote/ccd-cli/2.1.317-example"
            script = ("#!" + sys.executable + "\nimport json,os,sys\n"
                      "print(json.dumps({'args':sys.argv[1:], 'host_auth':os.environ.get('ANTHROPIC_AUTH_TOKEN'),"
                      " 'managed':os.environ.get('CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST'), 'home':os.environ.get('HOME')}))\n")
            for path in (runtime, remote, other):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(script)
                path.chmod(0o755)
            launcher = Path(directory) / "launcher"
            launcher.write_bytes(swap.render_launcher(os.getuid() if target_uid is None else target_uid,
                                                      home=str(home), gui_runtime=str(runtime) if runtime_enabled else None))
            launcher.chmod(0o755)
            command = str(remote if own_remote else other)
            args = ["space value", "'quoted'", "$(literal)", "", "--resume", "conversation-id"]
            env = dict(os.environ, ANTHROPIC_AUTH_TOKEN="discard", CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST="1",
                       HOME="/wrong-environment-home")
            result = subprocess.run([str(launcher), command, *args], env=env, text=True,
                                    capture_output=True, check=True)
            return json.loads(result.stdout), command, args

    def test_gui_runtime_routes_only_recorded_users_remote_cli_and_preserves_arguments(self):
        result, command, args = self.run_gui_launcher()
        self.assertEqual(result["args"], ["-m", "team_pool.gui_exec", "run", "--", command, *args])
        self.assertIsNone(result["host_auth"])
        self.assertIsNone(result["managed"])
        self.assertEqual(result["home"], "/wrong-environment-home")

    def test_gui_runtime_does_not_route_other_home_or_unenrolled_user(self):
        result, _, args = self.run_gui_launcher(own_remote=False)
        self.assertEqual(result["args"], args)
        result, _, args = self.run_gui_launcher(target_uid=os.getuid() + 10000)
        self.assertEqual(result["args"], args)
        self.assertEqual(result["host_auth"], "discard")
        self.assertEqual(result["managed"], "1")

    def test_personal_launcher_without_gui_runtime_keeps_direct_remote_exec(self):
        result, _, args = self.run_gui_launcher(runtime_enabled=False)
        self.assertEqual(result["args"], args)
        self.assertIsNone(result["host_auth"])

    def run_launcher(self, target_uid, restore_fast=False):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            launcher.write_bytes(swap.render_launcher(target_uid, restore_fast))
            launcher.chmod(0o755)
            names = swap.ENV_TO_CLEAR + ("CLAUDE_CODE_DISABLE_FAST_MODE", "KEEP_ME")
            env = dict(os.environ, **{name: "example" for name in names})
            program = "import json,os,sys; print(json.dumps({'args':sys.argv[1:],'env':{k:os.environ.get(k) for k in " + repr(names) + "}}))"
            result = subprocess.run([str(launcher), sys.executable, "-c", program, "space value", 'quoted"value', "$(literal)", ""], env=env, text=True, capture_output=True, check=True)
            return json.loads(result.stdout)

    def test_target_user_env_cleared_and_argv_preserved_without_enabling_fast(self):
        result = self.run_launcher(os.getuid())
        self.assertEqual(result["args"], ["space value", 'quoted"value', "$(literal)", ""])
        for name in swap.ENV_TO_CLEAR:
            self.assertIsNone(result["env"][name])
        self.assertEqual(result["env"]["CLAUDE_CODE_DISABLE_FAST_MODE"], "example")
        self.assertEqual(result["env"]["KEEP_ME"], "example")

    def test_other_user_env_preserved(self):
        result = self.run_launcher(os.getuid() + 10000, restore_fast=True)
        self.assertTrue(all(value == "example" for value in result["env"].values()))

    def test_fast_control_is_explicit_opt_in(self):
        result = self.run_launcher(os.getuid(), restore_fast=True)
        self.assertIsNone(result["env"]["CLAUDE_CODE_DISABLE_FAST_MODE"])
        self.assertEqual(result["env"]["KEEP_ME"], "example")

    def test_exit_status_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            launcher.write_bytes(swap.render_launcher(os.getuid()))
            launcher.chmod(0o755)
            result = subprocess.run([str(launcher), "/bin/sh", "-c", "exit 17"])
            self.assertEqual(result.returncode, 17)

    def test_shared_launcher_cannot_exceed_older_clients_read_limit(self):
        users = [{"uid": 501 + index, "restore_fast_control": False} for index in range(128)]
        with self.assertRaisesRegex(swap.SetupError, "Too many enrolled"):
            swap.render_users_launcher(users)


if __name__ == "__main__":
    unittest.main()
