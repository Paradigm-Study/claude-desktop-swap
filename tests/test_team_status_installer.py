"""Installer must leave unrelated apps and LaunchAgents untouched."""
import importlib.util
from pathlib import Path
import plistlib
import tempfile
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

if __name__ == "__main__":
    unittest.main()
