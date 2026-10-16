"""Offline tests: all configuration edits are confined to temporary directories."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
        with self.assertRaises(OSError):
            swap.install(self.layout)
        self.assertEqual(outside.read_text(), '{"untouched": true}')
        self.profile.unlink()
        self.save(self.profile, {})
        directory = self.layout.profiles
        moved = directory.with_name("real-configLibrary")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
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


class LauncherTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
