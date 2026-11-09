"""Offline launchd lifecycle tests; no credentials, native APIs, or live agents."""

import os
from pathlib import Path
import plistlib
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from team_pool import gui_exec_service as service
from team_pool.gui_exec import GUI_EXEC_REVISION


class FakeLaunchd:
    def __init__(self, destination):
        self.destination = destination
        self.loaded = None
        self.pid = 100
        self.calls = []
        self.health_calls = []
        self.active_jobs = 0
        self.draining = False
        self.bootstrap_failures = 0
        self.absent_bootstraps = 0
        self.print_changes = {}
        self.health_changes = {}
        self.native_ready = True
        self.unresponsive = False
        self.bootout_failure = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs["timeout"] == 3
        assert kwargs["check"] is False
        if argv[1] == "print":
            if self.loaded is None:
                return SimpleNamespace(returncode=113, stdout=b"")
            fields = {
                "path": str(self.destination), "type": "LaunchAgent", "state": "running",
                "program": self.loaded["ProgramArguments"][0],
                "working directory": self.loaded["WorkingDirectory"], "pid": str(self.pid),
            }
            fields.update(self.print_changes)
            text = "gui/fixture/service = {\n"
            text += "".join(f"\t{key} = {value}\n" for key, value in fields.items())
            text += "\targuments = {\n"
            text += "".join(f"\t\t{value}\n" for value in self.loaded["ProgramArguments"])
            text += "\t}\n}\n"
            return SimpleNamespace(returncode=0, stdout=text.encode())
        if argv[1] == "bootstrap":
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                return SimpleNamespace(returncode=5, stdout=b"")
            if self.absent_bootstraps:
                self.absent_bootstraps -= 1
                return SimpleNamespace(returncode=0, stdout=b"")
            self.loaded = plistlib.loads(Path(argv[3]).read_bytes())
            self.draining = False
            self.pid += 1
            return SimpleNamespace(returncode=0, stdout=b"")
        if argv[1] == "bootout":
            if self.bootout_failure:
                return SimpleNamespace(returncode=5, stdout=b"")
            assert self.draining and self.active_jobs == 0, "installer interrupted a job"
            self.loaded = None
            return SimpleNamespace(returncode=0, stdout=b"")
        raise AssertionError(argv)

    def health(self, path, *, timeout, op, expected_pid=None):
        self.health_calls.append(op)
        assert path == service.runtime_path(self.destination.parents[2]) / "socket"
        assert 0 < timeout <= 3
        if self.unresponsive or self.loaded is None:
            raise OSError("fixture-private-detail-must-not-escape")
        if op in ("prepare_stop", "resume") and expected_pid != self.pid:
            raise OSError("fixture-server-was-replaced")
        ok = True
        if op == "prepare_stop":
            if self.active_jobs:
                ok = False
            else:
                self.draining = True
        elif op == "resume":
            self.draining = False
        result = {
            "ok": ok, "pid": self.pid, "uid": os.getuid(),
            "active_jobs": self.active_jobs, "draining": self.draining,
            "module": "team_pool.gui_exec", "python": self.loaded["ProgramArguments"][0],
            "protocol_version": 1, "revision": self.loaded[service._REVISION_KEY],
        }
        if op == "check_ready":
            result.update(native_ready=self.native_ready, native_category="ready" if self.native_ready else "pool_unavailable")
        result.update(self.health_changes)
        return result


class GuiExecutionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir(mode=0o700)
        self.python = self.home / "venv/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("fixture")
        self.python.chmod(0o700)
        self.destination = service.service_path(self.home)
        self.launchd = FakeLaunchd(self.destination)
        self.now = 0
        # Test root runners can exercise ownership checks without allowing the
        # production API to install anything as root.
        self.uid_patch = patch.object(service.os, "geteuid", return_value=os.getuid() or 501)
        self.uid_patch.start()
        self.addCleanup(self.uid_patch.stop)

    def sleep(self, seconds):
        self.now += seconds

    def install(self, **changes):
        arguments = dict(home=self.home, python=str(self.python), runner=self.launchd,
                         health=self.launchd.health, platform="darwin", timeout=0.2,
                         clock=lambda: self.now, sleep=self.sleep)
        arguments.update(changes)
        return service.install_service(**arguments)

    def remove(self):
        return service.remove_service(home=self.home, runner=self.launchd, health=self.launchd.health, platform="darwin")

    def old_install(self, *, loaded=True):
        service._paths(self.home, create=True)
        old = service._spec(self.home, str(self.python), str(Path(service.__file__).absolute().parent.parent), "0.1.0a13")
        service._write(self.destination, plistlib.dumps(old))
        if loaded:
            self.launchd.loaded = old
        return self.destination.read_bytes()

    def mutations(self):
        return [argv[1] for argv in self.launchd.calls if argv[1] != "print"]

    def test_installs_owned_private_files_and_expected_arguments(self):
        result = self.install()
        self.assertEqual(result, {"status": "installed", "pid": 101})
        spec = plistlib.loads(self.destination.read_bytes())
        self.assertEqual(spec["ProgramArguments"], [str(self.python), "-m", "team_pool.gui_exec", "serve"])
        self.assertTrue(spec["KeepAlive"])
        self.assertTrue(spec["RunAtLoad"])
        self.assertEqual(spec["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(spec[service._REVISION_KEY], GUI_EXEC_REVISION)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        runtime = service.runtime_path(self.home)
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual((runtime / "service.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.mutations(), ["bootstrap"])
        self.assertIn("check_ready", self.launchd.health_calls)

    def test_venv_python_symlink_remains_lexical(self):
        target = self.python.with_name("python-real")
        self.python.rename(target)
        self.python.symlink_to(target.name)
        self.install()
        self.assertEqual(self.launchd.loaded["ProgramArguments"][0], str(self.python))

    def test_identical_healthy_install_keeps_active_jobs_running(self):
        self.install()
        self.launchd.calls.clear()
        self.launchd.health_calls.clear()
        self.launchd.active_jobs = 4
        self.assertEqual(self.install()["status"], "unchanged")
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.launchd.health_calls, ["check_ready"])
        self.assertFalse(self.launchd.draining)

    def test_upgrade_atomically_drains_old_and_checks_new_process(self):
        previous = self.old_install()
        result = self.install()
        self.assertEqual(result["status"], "updated")
        self.assertNotEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.mutations(), ["bootout", "bootstrap"])
        self.assertEqual(self.launchd.health_calls, ["status", "prepare_stop", "check_ready"])
        self.assertEqual(list(service.runtime_path(self.home).glob("rollback-*")), [])

    def test_upgrade_refuses_busy_service_before_any_mutation(self):
        previous = self.old_install()
        self.launchd.active_jobs = 1
        with self.assertRaisesRegex(service.GuiServiceError, "active Claude sessions"):
            self.install()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.launchd.draining)

    def test_bootstrap_failure_restores_old_plist_and_running_service(self):
        previous = self.old_install()
        self.launchd.bootstrap_failures = 1
        with self.assertRaisesRegex(service.GuiServiceError, "could not start"):
            self.install()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.launchd.loaded[service._REVISION_KEY], "0.1.0a13")
        self.assertEqual(self.mutations(), ["bootout", "bootstrap", "bootstrap"])
        self.assertEqual(list(service.runtime_path(self.home).glob("rollback-*")), [])

    def test_bootstrap_readiness_timeout_rolls_back_absent_new_service(self):
        previous = self.old_install()
        self.launchd.absent_bootstraps = 1
        with self.assertRaisesRegex(service.GuiServiceError, "deadline"):
            self.install()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.launchd.loaded[service._REVISION_KEY], "0.1.0a13")
        self.assertGreaterEqual(self.now, 0.2)

    def test_new_install_failure_removes_only_its_new_plist(self):
        self.launchd.bootstrap_failures = 1
        with self.assertRaises(service.GuiServiceError):
            self.install()
        self.assertFalse(self.destination.exists())

    def test_failure_does_not_start_previously_unloaded_old_service(self):
        previous = self.old_install(loaded=False)
        self.launchd.bootstrap_failures = 1
        with self.assertRaises(service.GuiServiceError):
            self.install()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertIsNone(self.launchd.loaded)
        self.assertEqual(self.mutations(), ["bootstrap"])

    def test_native_unreadable_service_is_not_reported_ready(self):
        self.launchd.native_ready = False
        with self.assertRaisesRegex(service.GuiServiceError, "deadline"):
            self.install()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.mutations(), ["bootstrap", "bootout"])

    def test_unresponsive_upgrade_does_not_interrupt_or_overwrite(self):
        previous = self.old_install()
        self.launchd.unresponsive = True
        with self.assertRaisesRegex(service.GuiServiceError, "no running jobs were interrupted"):
            self.install()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.mutations(), [])

    def test_unresponsive_new_service_retains_previous_recovery_file(self):
        previous = self.old_install(loaded=False)
        self.launchd.unresponsive = True
        with self.assertRaisesRegex(service.GuiServiceError, "Recovery files were retained"):
            self.install()
        backups = list(service.runtime_path(self.home).glob("rollback-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), previous)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.mutations(), ["bootstrap"])

    def test_bootout_failure_resumes_old_service_and_keeps_plist(self):
        previous = self.old_install()
        self.launchd.bootout_failure = True
        with self.assertRaisesRegex(service.GuiServiceError, "could not be stopped"):
            self.install()
        self.assertFalse(self.launchd.draining)
        self.assertEqual(self.launchd.health_calls, ["status", "prepare_stop", "resume"])
        self.assertEqual(self.destination.read_bytes(), previous)

    def test_foreign_loaded_agent_without_plist_is_not_touched(self):
        self.old_install()
        self.destination.unlink()
        with self.assertRaisesRegex(service.GuiServiceError, "unowned"):
            self.install()
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.destination.exists())

    def test_foreign_loaded_path_runtime_and_arguments_are_not_touched(self):
        for field, value in (("path", "/foreign.plist"), ("program", "/bin/sh"),
                             ("working directory", "/tmp"), ("type", "LaunchDaemon")):
            with self.subTest(field=field):
                previous = self.old_install()
                self.launchd.print_changes = {field: value}
                with self.assertRaisesRegex(service.GuiServiceError, "unrecognized path or runtime"):
                    self.install()
                self.assertEqual(self.destination.read_bytes(), previous)
                self.assertEqual(self.mutations(), [])
                self.assertFalse(self.launchd.draining)

    def test_health_must_match_loaded_process_and_runtime(self):
        for field, value in (("pid", 10000), ("uid", os.getuid() + 1),
                             ("python", "/bin/sh"), ("revision", "foreign"),
                             ("module", "other"), ("protocol_version", True)):
            with self.subTest(field=field):
                previous = self.old_install()
                self.launchd.health_changes = {field: value}
                with self.assertRaisesRegex(service.GuiServiceError, "could not safely stop"):
                    self.install()
                self.assertEqual(self.destination.read_bytes(), previous)
                self.assertEqual(self.mutations(), [])

    def test_malformed_and_foreign_plists_are_preserved(self):
        self.old_install()
        foreign = dict(self.launchd.loaded, ProgramArguments=["/bin/sh", "-c", "private-data"])
        malformed = dict(self.launchd.loaded, KeepAlive=1)
        for raw in (b"not-a-plist", plistlib.dumps(foreign), plistlib.dumps(["unexpected"]), plistlib.dumps(malformed)):
            with self.subTest(raw=raw):
                service._write(self.destination, raw)
                with self.assertRaisesRegex(service.GuiServiceError, "unrecognized"):
                    self.install()
                self.assertEqual(self.destination.read_bytes(), raw)
                self.assertEqual(self.mutations(), [])

    def test_service_symlink_is_not_overwritten(self):
        previous = self.old_install()
        target = self.destination.with_name("foreign.plist")
        self.destination.rename(target)
        self.destination.symlink_to(target)
        with self.assertRaisesRegex(service.GuiServiceError, "unsafe"):
            self.install()
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(self.mutations(), [])

    def test_nonprivate_plist_is_not_changed(self):
        previous = self.old_install()
        self.destination.chmod(0o644)
        with self.assertRaisesRegex(service.GuiServiceError, "mode 0600"):
            self.install()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o644)

    def test_foreign_file_owner_is_rejected(self):
        self.old_install()
        real_fstat = os.fstat
        target_inode = self.destination.stat().st_ino

        def foreign(fd):
            info = real_fstat(fd)
            if info.st_ino == target_inode:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)
            return info

        with patch.object(service.os, "fstat", side_effect=foreign):
            with self.assertRaises(service.GuiServiceError):
                self.install()
        self.assertEqual(self.mutations(), [])

    def test_runtime_symlink_or_public_directory_is_rejected(self):
        self.old_install()
        runtime = service.runtime_path(self.home)
        runtime.chmod(0o755)
        with self.assertRaisesRegex(service.GuiServiceError, "mode 0700"):
            self.install()
        runtime.chmod(0o700)
        target = runtime.with_name("other-runtime")
        runtime.rename(target)
        runtime.symlink_to(target)
        with self.assertRaisesRegex(service.GuiServiceError, "safe directories"):
            self.install()
        self.assertEqual(self.mutations(), [])

    def test_log_symlink_is_rejected_before_service_change(self):
        previous = self.old_install()
        log = service.runtime_path(self.home) / "service.log"
        target = self.home / "unrelated"
        target.write_text("retain")
        log.symlink_to(target)
        with self.assertRaisesRegex(service.GuiServiceError, "logs"):
            self.install()
        self.assertEqual(target.read_text(), "retain")
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.mutations(), [])

    def test_removal_drains_only_owned_idle_service(self):
        self.old_install()
        self.assertEqual(self.remove(), {"status": "removed"})
        self.assertFalse(self.destination.exists())
        self.assertIsNone(self.launchd.loaded)
        self.assertEqual(self.launchd.health_calls, ["status", "prepare_stop"])
        self.assertEqual(self.mutations(), ["bootout"])

    def test_removal_refuses_active_jobs(self):
        previous = self.old_install()
        self.launchd.active_jobs = 1
        with self.assertRaisesRegex(service.GuiServiceError, "active Claude sessions"):
            self.remove()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.mutations(), [])

    def test_failed_plist_write_restores_previously_running_service(self):
        previous = self.old_install()
        real_write = service._write
        calls = 0

        def fail_first(path, raw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise service.GuiServiceError("Fixture write failed.")
            return real_write(path, raw)

        with patch.object(service, "_write", side_effect=fail_first):
            with self.assertRaisesRegex(service.GuiServiceError, "Fixture write failed"):
                self.install()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.launchd.loaded[service._REVISION_KEY], "0.1.0a13")
        self.assertEqual(self.mutations(), ["bootout", "bootstrap"])

    def test_job_arriving_after_status_is_not_interrupted(self):
        previous = self.old_install()
        real_health = self.launchd.health

        def concurrent_job(path, **kwargs):
            if kwargs["op"] == "prepare_stop":
                self.launchd.active_jobs = 1
            return real_health(path, **kwargs)

        with self.assertRaisesRegex(service.GuiServiceError, "active Claude sessions"):
            self.install(health=concurrent_job)
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.launchd.draining)

    def test_replacement_server_is_not_drained_after_status(self):
        previous = self.old_install()
        real_health = self.launchd.health

        def replacement(path, **kwargs):
            if kwargs["op"] == "prepare_stop":
                self.launchd.pid += 1
            return real_health(path, **kwargs)

        with self.assertRaisesRegex(service.GuiServiceError, "could not safely stop"):
            self.install(health=replacement)
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.launchd.draining)

    def test_lost_drain_response_resumes_same_server(self):
        previous = self.old_install()
        real_health = self.launchd.health

        def lost_response(path, **kwargs):
            value = real_health(path, **kwargs)
            if kwargs["op"] == "prepare_stop":
                raise TimeoutError("fixture response lost after applying drain")
            return value

        with self.assertRaisesRegex(service.GuiServiceError, "could not safely stop"):
            self.install(health=lost_response)
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.launchd.health_calls, ["status", "prepare_stop", "resume"])
        self.assertFalse(self.launchd.draining)

    def test_removal_of_absent_service_is_noop(self):
        self.assertEqual(self.remove(), {"status": "absent"})
        self.assertEqual(self.mutations(), [])

    def test_refuses_root_and_other_platforms(self):
        with patch.object(service.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(service.GuiServiceError, "without sudo"):
                self.install()
        with self.assertRaisesRegex(service.GuiServiceError, "requires macOS"):
            self.install(platform="linux")
        self.assertEqual(self.launchd.calls, [])


if __name__ == "__main__":
    unittest.main()
