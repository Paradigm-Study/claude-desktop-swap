"""Run the actual shell bootstrap against isolated command and download fixtures."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPOSITORY_ROOT / "install.sh"

# Only external Mac commands, network, and the Python environment manager are
# replaced. Filesystem operations, shell control flow, tar validation, cleanup,
# lock handling, and ready-marker behavior execute from the actual installer.
DISPATCH = r'''#!PYTHON
import json
import os
from pathlib import Path
import shutil
import stat
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
log = Path(os.environ["BOOTSTRAP_FIXTURE_LOG"])
with log.open("a") as stream:
    stream.write(json.dumps({"command": name, "args": args, "cwd": os.getcwd(),
                            "gateway_env_present": any(key in os.environ for key in (
                                "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"))}) + "\n")
if name == "uname":
    print(os.environ.get("BOOTSTRAP_FAKE_OS", "Darwin"))
elif name == "id":
    print(os.environ.get("BOOTSTRAP_FAKE_UID", "501"))
elif name == "stat":
    print(format(stat.S_IMODE(Path(args[-1]).stat().st_mode), "o"))
elif name in ("xcode-select", "xcrun"):
    print("/fixture/CommandLineTools")
elif name == "curl":
    output = Path(args[args.index("--output") + 1])
    if os.environ.get("BOOTSTRAP_FETCH_FAIL"):
        output.write_bytes(b"partial interrupted download")
        raise SystemExit(22)
    shutil.copyfile(os.environ["BOOTSTRAP_FIXTURE_ARCHIVE"], output)
elif name == "uv":
    if args[0] == "venv":
        runtime = Path(args[-1]) / "bin/python"
        runtime.parent.mkdir(parents=True)
        shutil.copyfile(os.environ["BOOTSTRAP_FIXTURE_DISPATCH"], runtime)
        runtime.chmod(0o700)
    elif args[:2] != ["pip", "install"]:
        raise SystemExit("unexpected uv command")
elif name == "python":
    if args[:2] == ["-c", "import team_pool.setup"]:
        pass
    elif args[:2] == ["-m", "team_pool.setup"]:
        if os.environ.get("BOOTSTRAP_ORCHESTRATOR_FAIL"):
            raise SystemExit(2)
    else:
        raise SystemExit("unexpected runtime invocation")
else:
    raise SystemExit("unexpected external command")
'''


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pool-bootstrap-test-")
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name).resolve()
        self.home = self.fixture / "fixture home"
        self.home.mkdir(mode=0o700)
        self.account = self.home / ".claude/shared-session/account.json"
        self.account.parent.mkdir(parents=True)
        self.account.write_text('{"fixture": true}\n')
        self.account.chmod(0o600)
        (self.home / "Applications/Claude.app").mkdir(parents=True)
        self.fakebin = self.fixture / "bin"
        self.fakebin.mkdir()
        self.dispatch = self.fakebin / "dispatch"
        self.dispatch.write_text(DISPATCH.replace("#!PYTHON", "#!" + sys.executable, 1))
        self.dispatch.chmod(0o700)
        self.log = self.fixture / "commands.jsonl"
        self.archive = self.fixture / "release.tar.gz"
        self.make_archive()
        source = BOOTSTRAP.read_text()
        # Never replace the real HOME environment variable. Redirect only the
        # test copy's home references to an explicit, temporary fixture root.
        source = source.replace("$HOME", "${BOOTSTRAP_FIXTURE_HOME}")
        for command in ("uname", "id", "stat", "curl", "xcode-select", "xcrun"):
            fake = self.fakebin / command
            shutil.copyfile(self.dispatch, fake)
            fake.chmod(0o700)
            source = source.replace("/usr/bin/" + command, str(fake))
        shutil.copyfile(self.dispatch, self.fakebin / "uv")
        (self.fakebin / "uv").chmod(0o700)
        self.script = self.fixture / "install.sh"
        self.script.write_text(source)
        self.script.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update(BOOTSTRAP_FIXTURE_HOME=str(self.home),
                        BOOTSTRAP_FIXTURE_LOG=str(self.log),
                        BOOTSTRAP_FIXTURE_ARCHIVE=str(self.archive),
                        BOOTSTRAP_FIXTURE_DISPATCH=str(self.dispatch),
                        PATH=str(self.fakebin) + os.pathsep + self.env.get("PATH", ""))
        # The installer explicitly rejects these overrides. Unrelated user test
        # environments must not decide fixture outcomes or leak into logs.
        for key in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            self.env.pop(key, None)
        self.root = self.home / ".local/share/claude-desktop-swap"

    def make_archive(self, *, traversal=False):
        with tarfile.open(self.archive, "w:gz") as archive:
            entries = {"claude-desktop-swap-fixture/pyproject.toml": b"# package fixture\n",
                       "claude-desktop-swap-fixture/team_pool/setup.py": b"# setup fixture\n"}
            if traversal:
                entries["../outside-download"] = b"must never extract\n"
            for name, data in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(data))

    def run_script(self, *args, **overrides):
        env = dict(self.env, **overrides)
        return subprocess.run(["/bin/bash", str(self.script), *args], cwd=self.fixture,
                              env=env, capture_output=True, text=True, timeout=20)

    def events(self, name=None):
        events = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [event for event in events if name is None or event["command"] == name]

    def release(self):
        paths = list((self.root / "releases").iterdir())
        self.assertEqual(len(paths), 1)
        return paths[0]

    def assert_clean(self):
        self.assertFalse((self.root / ".bootstrap-lock").exists())
        self.assertEqual(list(self.root.glob(".download.*")), [])

    def make_lock(self, pid):
        lock = self.root / ".bootstrap-lock"
        lock.mkdir(parents=True, mode=0o700)
        (lock / "pid").write_text(str(pid) + "\n")
        (lock / "pid").chmod(0o600)
        return lock

    def test_shell_syntax_and_help_need_no_mac_or_login(self):
        syntax = subprocess.run(["/bin/bash", "-n", str(BOOTSTRAP)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.account.unlink()
        result = self.run_script("--help", BOOTSTRAP_FAKE_OS="Linux", BOOTSTRAP_FAKE_UID="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: install.sh", result.stdout)
        self.assertEqual(self.events(), [])
        self.assertFalse(self.root.exists())

    def test_non_mac_and_root_fail_before_any_download(self):
        for override, message in (({"BOOTSTRAP_FAKE_OS": "Linux"}, "requires macOS"),
                                  ({"BOOTSTRAP_FAKE_UID": "0"}, "without sudo")):
            with self.subTest(override=override):
                result = self.run_script(**override)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.events("curl"), [])
                self.assertEqual(self.events("python"), [])
                self.assertFalse(self.root.exists())

    def test_missing_sign_in_fails_before_download(self):
        self.account.unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Sign into the Paradigm team", result.stderr)
        self.assertEqual(self.events("curl"), [])
        self.assertEqual(self.events("python"), [])
        self.assertFalse(self.root.exists())

    def test_fresh_setup_runs_module_from_persistent_release_and_cleans_lock(self):
        result = self.run_script(ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_BASE_URL="https://fixture.invalid")
        self.assertEqual(result.returncode, 0, result.stderr)
        release = self.release()
        self.assertEqual((release / ".ready").read_text().strip(), release.name)
        self.assertTrue((release / "source/team_pool/setup.py").is_file())
        self.assertTrue((release / "venv/bin/python").is_file())
        self.assertEqual(len(self.events("curl")), 1)
        runtime = self.events("python")[-1]
        self.assertEqual(runtime["args"], ["-m", "team_pool.setup", "--source-dir", str(release / "source"),
                                           "--account-file", str(self.account)])
        self.assertEqual(runtime["cwd"], str(release))
        self.assertFalse(runtime["gateway_env_present"])
        self.assertEqual([event["args"][0] for event in self.events("uv")], ["venv", "pip"])
        self.assert_clean()

    def test_ready_release_rerun_skips_fetch_and_preserves_runtime(self):
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        release = self.release()
        runtime = release / "venv/bin/python"
        before = (runtime.stat().st_ino, runtime.read_bytes())
        sentinel = release / "keep-existing-runtime"
        sentinel.write_text("a running service may use this release")
        downloads, uv_calls = len(self.events("curl")), len(self.events("uv"))
        result = self.run_script("--check", BOOTSTRAP_FETCH_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((runtime.stat().st_ino, runtime.read_bytes()), before)
        self.assertTrue(sentinel.exists())
        self.assertEqual(len(self.events("curl")), downloads)
        self.assertEqual(len(self.events("uv")), uv_calls)
        self.assertEqual(self.events("python")[-1]["args"][-1], "--check")
        self.assert_clean()

    def test_partial_download_failure_retries_without_marking_or_running_partial_release(self):
        result = self.run_script(BOOTSTRAP_FETCH_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        release = self.release()
        self.assertTrue((release / ".owner").exists())
        self.assertFalse((release / ".ready").exists())
        self.assertFalse((release / "venv").exists())
        self.assertEqual(self.events("python"), [])
        self.assert_clean()
        failed_marker = release / "partial-marker"
        failed_marker.write_text("discard only the incomplete release")
        retry = self.run_script()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertFalse(failed_marker.exists())
        self.assertTrue((release / ".ready").exists())
        self.assertEqual(len(self.events("curl")), 2)
        module_runs = [event for event in self.events("python") if event["args"][:1] == ["-m"]]
        self.assertEqual(len(module_runs), 1)
        self.assert_clean()

    def test_running_setup_lock_is_preserved_without_download(self):
        lock = self.make_lock(os.getpid())
        before = (lock / "pid").read_bytes()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already running", result.stderr)
        self.assertEqual((lock / "pid").read_bytes(), before)
        self.assertEqual(self.events("curl"), [])
        self.assertEqual(self.events("python"), [])

    def test_stale_setup_lock_is_recovered_and_cleaned(self):
        stale_pid = 99999999
        with self.assertRaises(ProcessLookupError):
            os.kill(stale_pid, 0)
        self.make_lock(stale_pid)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.release() / ".ready").exists())
        self.assert_clean()

    def test_archive_with_multiple_roots_is_rejected_before_extraction(self):
        with tarfile.open(self.archive, "w:gz") as archive:
            for name in ("first-root/pyproject.toml", "second-root/team_pool/setup.py"):
                data = b"# distinct source roots must not be merged\n"
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(data))
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source root", result.stderr)
        release = self.release()
        self.assertFalse((release / "source").exists())
        self.assertFalse((release / ".ready").exists())
        self.assertEqual(self.events("uv"), [])
        self.assertEqual(self.events("python"), [])
        self.assert_clean()

    def test_archive_traversal_is_rejected_before_extraction(self):
        self.make_archive(traversal=True)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe path", result.stderr)
        release = self.release()
        self.assertFalse((release / "source").exists())
        self.assertFalse((release / ".ready").exists())
        self.assertEqual(self.events("uv"), [])
        self.assertEqual(self.events("python"), [])
        self.assertEqual(list(self.fixture.rglob("outside-download")), [])
        self.assert_clean()

    def test_orchestrator_failure_keeps_ready_runtime_and_releases_lock(self):
        result = self.run_script(BOOTSTRAP_ORCHESTRATOR_FAIL="1")
        self.assertEqual(result.returncode, 2)
        release = self.release()
        self.assertTrue((release / ".ready").is_file())
        self.assertTrue((release / "venv/bin/python").is_file())
        self.assert_clean()
        retry = self.run_script()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(len(self.events("curl")), 1)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
