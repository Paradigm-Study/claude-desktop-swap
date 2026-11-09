"""Real local subprocesses exercise the native GUI relay's process contract."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from team_pool import gui_exec as bridge


class GUIExecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / ".claude/remote/ccd-cli"
        self.root.mkdir(parents=True)
        self.path = self.home / "runtime/socket"
        self.path.parent.mkdir(mode=0o700)
        self.server = bridge.Server(self.path, home=self.home, allowed_root=self.root, pool_check=lambda: None)
        self.server.bind()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.close()
        self.thread.join(timeout=3)
        deadline = time.monotonic() + 3
        while self.server.connections and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.server.connections)

    def cli(self, body, name="version"):
        executable = self.root / name
        executable.write_text(f"#!{sys.executable}\n" + body)
        executable.chmod(0o700)
        return executable

    def relay(self, executable, *args, env=None):
        code = ("from team_pool.gui_exec import run, BridgeError; import sys; "
                f"sys.exit(run({[str(executable), *args]!r}, path={str(self.path)!r}, home={str(self.home)!r}))")
        result = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: result.kill() if result.poll() is None else None)
        return result

    def connect(self):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(3)
        conn.connect(str(self.path))
        self.addCleanup(conn.close)
        return conn

    def request(self, executable, env=None):
        return {"argv": [str(executable)], "cwd": str(self.home), "env": env or {}}

    def wait_file(self, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return path.read_text()
            time.sleep(0.02)
        self.fail("Child did not reach fixture checkpoint")

    def assert_dead(self, pid):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            proc = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True)
            if proc.returncode or proc.stdout.strip().startswith(b"Z"):
                return
            time.sleep(0.03)
        self.fail(f"Owned child {pid} survived cancellation")

    def test_binary_stream_stdin_eof_stderr_and_exit(self):
        executable = self.cli("import sys\ndata=sys.stdin.buffer.read()\nsys.stdout.buffer.write(data[::-1])\nsys.stderr.buffer.write(b'error\\x00\\xff')\nsys.exit(7)\n")
        payload = bytes(range(256)) * 5000  # Many flow-control windows.
        process = self.relay(executable)
        out, err = process.communicate(payload, timeout=15)
        self.assertEqual(process.returncode, 7)
        self.assertEqual(out, payload[::-1])
        self.assertEqual(err, b"error\x00\xff")

    def test_child_can_exit_without_consuming_large_stdin(self):
        executable = self.cli("import sys\nprint('done')\nsys.exit(42)\n")
        process = self.relay(executable)
        out, err = process.communicate(b"x" * 2000000, timeout=5)
        self.assertEqual((process.returncode, out, err), (42, b"done\n", b""))

    def test_output_backpressure_does_not_block_independent_connection(self):
        executable = self.cli("import os\nwhile True: os.write(1,b'x'*32768)\n")
        flood = self.relay(executable)
        self.addCleanup(flood.stdout.close)
        time.sleep(0.15)
        other = self.relay(self.cli("print('independent')\n", "second"))
        out, err = other.communicate(timeout=5)
        self.assertEqual((out, err, other.returncode), (b"independent\n", b"", 0))
        flood.kill()
        flood.communicate(timeout=5)

    def test_disconnect_cancels_process_and_descendant(self):
        checkpoint = self.home / "pids"
        executable = self.cli("import os,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
            f"open({str(checkpoint)!r},'w').write(str(os.getpid())+' '+str(child.pid))\n"
            "time.sleep(120)\n")
        process = self.relay(executable)
        pids = [int(x) for x in self.wait_file(checkpoint).split()]
        process.kill()
        process.communicate(timeout=5)
        for pid in pids:
            self.assert_dead(pid)

    def test_server_shutdown_cancels_owned_process_before_returning(self):
        checkpoint = self.home / "pid"
        executable = self.cli(f"import os,time\nopen({str(checkpoint)!r},'w').write(str(os.getpid()))\ntime.sleep(120)\n")
        process = self.relay(executable)
        pid = int(self.wait_file(checkpoint))
        self.server.close()
        self.assert_dead(pid)
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)

    def test_signal_is_forwarded_and_normal_result_preserved(self):
        checkpoint = self.home / "ready"
        executable = self.cli("import signal,time,sys\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(23))\n"
            f"open({str(checkpoint)!r},'w').write('ready')\n"
            "time.sleep(120)\n")
        process = self.relay(executable)
        self.wait_file(checkpoint)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=5)
        self.assertEqual((process.returncode, out, err), (23, b"", b""))

    def test_health_drain_is_atomic_with_job_registration(self):
        value = bridge.health(self.path, home=self.home)
        self.assertEqual(value["protocol_version"], bridge.PROTOCOL_VERSION)
        self.assertEqual(value["revision"], bridge.GUI_EXEC_REVISION)
        self.assertEqual(value["active_jobs"], 0)
        self.assertTrue(bridge.health(self.path, home=self.home, op="check_ready")["native_ready"])
        checkpoint = self.home / "ready"
        executable = self.cli(f"import time\nopen({str(checkpoint)!r},'w').write('ready')\ntime.sleep(120)\n")
        process = self.relay(executable)
        self.wait_file(checkpoint)
        busy = bridge.health(self.path, home=self.home, op="prepare_stop", expected_pid=value["pid"])
        self.assertFalse(busy["ok"])
        self.assertFalse(busy["draining"])
        process.terminate()
        process.communicate(timeout=5)
        deadline = time.monotonic() + 3
        while bridge.health(self.path, home=self.home)["active_jobs"] and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(bridge.health(self.path, home=self.home, op="prepare_stop", expected_pid=value["pid"])["draining"])
        rejected = self.relay(executable)
        out, err = rejected.communicate(timeout=5)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"too many active", err)
        self.assertFalse(bridge.health(self.path, home=self.home, op="resume", expected_pid=value["pid"])["draining"])

    def test_health_mutations_require_the_observed_server_pid(self):
        value = bridge.health(self.path, home=self.home)
        for op in ("prepare_stop", "resume"):
            for expected in (None, True, value["pid"] + 1):
                with self.subTest(op=op, expected=expected):
                    with self.assertRaises(bridge.BridgeError):
                        bridge.health(self.path, home=self.home, op=op, expected_pid=expected)
                    self.assertFalse(bridge.health(self.path, home=self.home)["draining"])
        bridge.health(self.path, home=self.home, op="prepare_stop", expected_pid=value["pid"])
        with self.assertRaises(bridge.BridgeError):
            bridge.health(self.path, home=self.home, op="resume", expected_pid=value["pid"] + 1)
        self.assertTrue(bridge.health(self.path, home=self.home)["draining"])
        bridge.health(self.path, home=self.home, op="resume", expected_pid=value["pid"])

    def test_wrong_peer_uid_rejected_before_parse(self):
        self.server.peer_check = lambda _: os.getuid() + 1
        conn = self.connect()
        conn.sendall(b"secret-not-a-valid-request")
        try:
            self.assertEqual(conn.recv(100), b"")
        except ConnectionResetError:
            pass
        self.assertEqual(self.server.active_jobs, 0)

    def test_oversized_frame_rejected_without_payload(self):
        conn = self.connect()
        conn.sendall(b"J" + struct.pack("!I", bridge.MAX_FRAME + 1))
        value = conn.recv(200)
        self.assertIn(b"invalid_request", value)

    def test_failures_do_not_echo_secrets(self):
        self.server.pool_check = lambda: (_ for _ in ()).throw(RuntimeError("synthetic-provider-secret"))
        conn = self.connect()
        executable = self.cli("print('must not run')\n")
        conn.sendall(bridge._frame(b"J", bridge._json(self.request(executable))))
        output = conn.recv(200)
        self.assertNotIn(b"synthetic-provider-secret", output)
        self.assertIn(b"execution_failed", output)
        self.assertFalse(bridge.health(self.path, home=self.home, op="check_ready")["native_ready"])

    def test_validates_exact_owned_cli_root_and_file(self):
        executable = self.cli("pass\n")
        def validate():
            return bridge.validate_request(self.request(executable), allowed_root=self.root,
                home=self.home, uid=os.getuid())
        validate()
        executable.chmod(0o722)
        with self.assertRaises(bridge.BridgeError): validate()
        executable.chmod(0o700)
        executable.unlink()
        executable.symlink_to(sys.executable)
        with self.assertRaises(bridge.BridgeError): validate()
        arbitrary = self.home / "not-claude"
        arbitrary.write_text("never")
        arbitrary.chmod(0o700)
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_request(self.request(arbitrary), allowed_root=self.root, home=self.home, uid=os.getuid())

    def test_auth_environment_scrubbed_before_wire_and_again_before_spawn(self):
        source = {"ANTHROPIC_API_KEY": "secret", "ANTHROPIC_AUTH_TOKEN": "secret", "ANTHROPIC_BASE_URL": "secret",
            "CLAUDE_CODE_HOST_AUTH_ENV_VAR": "CUSTOM_PROVIDER_AUTH", "CUSTOM_PROVIDER_AUTH": "secret",
            "CLAUDE_CODE_OAUTH_TOKEN": "secret", "CLAUDE_CONFIG_DIR": "/other-user",
            "CLAUDE_CODE_SECURE_STORE_DIR": "/other-store", "DYLD_INSERT_LIBRARIES": "secret",
            "HOME": "/other-user", "USER": "other", "LOGNAME": "other", "PATH": "/usr/bin:/bin", "TERM": "dumb"}
        clean = bridge.sanitized_environment(source, home=self.home)
        self.assertNotIn("secret", json.dumps(clean))
        self.assertEqual(clean["HOME"], str(self.home))
        self.assertNotEqual(clean["USER"], "other")
        executable = self.cli("import os,json\nprint(json.dumps(dict(os.environ)))\n")
        conn = self.connect()
        conn.sendall(bridge._frame(b"J", bridge._json(self.request(executable, source))))
        incoming, output = bytearray(), bytearray()
        finished = False
        while not finished:
            incoming.extend(conn.recv(32768))
            while (frame := bridge._take(incoming)) is not None:
                kind, data = frame
                if kind == b"O": output.extend(data)
                if kind == b"X": finished = True
        self.assertNotIn(b"secret", output)
        self.assertEqual(json.loads(output)["HOME"], str(self.home))

    def test_missing_socket_never_falls_back_to_direct_execution(self):
        checkpoint = self.home / "not-created"
        executable = self.cli(f"open({str(checkpoint)!r},'w').write('unsafe')\n")
        with self.assertRaisesRegex(bridge.BridgeError, "signed in"):
            bridge.run([str(executable)], path=self.path.parent / "absent", home=self.home, install_signals=False)
        self.assertFalse(checkpoint.exists())

    def test_runtime_symlink_ancestor_refused_before_file_creation(self):
        other = self.home / "other"
        other.mkdir()
        redirected = self.home / "redirect"
        redirected.symlink_to(other, target_is_directory=True)
        with self.assertRaises(bridge.BridgeError):
            bridge._runtime_directory(redirected / "new", os.getuid(), create=True, home=self.home)
        self.assertFalse((other / "new").exists())
        other.chmod(0o777)
        with self.assertRaises(bridge.BridgeError):
            bridge._runtime_directory(other / "new", os.getuid(), create=True, home=self.home)
        self.assertFalse((other / "new").exists())
        other.chmod(0o700)

    def test_non_socket_or_permissive_socket_rejected(self):
        ordinary = self.path.parent / "file"
        ordinary.write_text("preserved")
        ordinary.chmod(0o600)
        with self.assertRaises(bridge.BridgeError): bridge.health(ordinary, home=self.home)
        self.assertEqual(ordinary.read_text(), "preserved")
        self.path.chmod(0o666)
        with self.assertRaises(bridge.BridgeError): bridge.health(self.path, home=self.home)
        self.path.chmod(0o600)


class PoolGuardTests(unittest.TestCase):
    @contextmanager
    def guarded(self):
        self.guarding = True
        try:
            yield
        finally:
            self.guarding = False

    def setUp(self):
        self.guarding = False
        self.state = {"state": "active", "managed": True, "expires_at": 1000,
                      "fingerprint": "owned", "synced_at": 50}
        self.snapshot = {"has_credentials": True, "fingerprint": "owned"}
        self.native = SimpleNamespace(noninteractive=self.guarded, preflight=self.read_native,
                                      snapshot=lambda: self.read_native() or self.snapshot)
        self.client = SimpleNamespace(_load=lambda: dict(self.state), native=self.native, clock=lambda: 100)
        self.mock = patch("team_pool.client.Client", return_value=self.client)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def read_native(self):
        if not self.guarding:
            raise AssertionError("Keychain access must be noninteractive")

    def test_owned_current_assignment_passes_guard_without_network(self):
        bridge.check_pool()
        self.assertFalse(self.guarding)

    def test_expired_paused_unmanaged_pending_or_drifted_fail_closed(self):
        for changes in ({"expires_at": 120}, {"expires_at": float('nan')},
                        {"state": "paused"}, {"managed": False}, {"pending_write": {}},
                        {"fingerprint": "someone-else"}):
            original = dict(self.state)
            self.state.update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(bridge.BridgeError, "complete native pool sync"):
                bridge.check_pool()
            self.state = original

    def test_concurrent_native_checks_never_overlap_interaction_policy(self):
        active, maximum = 0, 0
        counter_lock = threading.Lock()
        @contextmanager
        def guarded():
            nonlocal active, maximum
            with counter_lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.03)
                yield
            finally:
                with counter_lock:
                    active -= 1
        self.native.noninteractive = guarded
        self.native.preflight = lambda: None
        self.native.snapshot = lambda: self.snapshot
        failures = []
        def check():
            try: bridge.check_pool()
            except Exception as exc: failures.append(exc)
        workers = [threading.Thread(target=check) for _ in range(4)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=3)
        self.assertEqual(maximum, 1)
        self.assertEqual(failures, [])

    def test_native_error_and_concurrent_state_change_fail_without_secret(self):
        self.native.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("synthetic-secret"))
        with self.assertRaises(bridge.BridgeError) as raised:
            bridge.check_pool()
        self.assertNotIn("synthetic-secret", str(raised.exception))
        self.native.snapshot = lambda: self.snapshot
        calls = iter([self.state, {**self.state, "fingerprint": "changed"}])
        self.client._load = lambda: next(calls)
        with self.assertRaises(bridge.BridgeError):
            bridge.check_pool()


if __name__ == "__main__":
    unittest.main()
