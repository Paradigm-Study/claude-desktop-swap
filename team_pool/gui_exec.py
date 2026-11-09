"""Run Desktop's remote native CLI inside its user's GUI login session.

The socket transports process IO, never a fetched account credential. Only the
GUI LaunchAgent starts Claude; Claude reads its normal Keychain itself. A logged
out Mac deliberately fails closed instead of falling back to SSH authentication.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
import pwd
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

SERVICE = "study.paradigm.claude-desktop-swap.gui-exec"
DEFAULT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
RUNTIME = DEFAULT_HOME / ".config/claude-desktop-swap/gui-exec"
SOCKET = RUNTIME / "socket"
CHUNK = 32768
WINDOW = 65536
MAX_FRAME = 512 * 1024
MAX_OUTPUT = 256 * 1024
MAX_CONNECTIONS = 64
MAX_JOBS = 32
PROTOCOL_VERSION = 1
GUI_EXEC_REVISION = "0.1.0a14"
_NATIVE_GUARD_LOCK = threading.Lock()
SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
MESSAGES = {
    "gui_required": "Claude Swap needs this macOS user signed in on the remote Mac's screen. Stay logged in and rerun pool setup if the GUI execution service is missing.",
    "pool_unavailable": "Claude Swap cannot use the local subscription yet. On the remote Mac's screen, check team-pool-client status and complete native pool sync before retrying.",
    "invalid_request": "Claude Swap rejected an invalid remote execution request.",
    "busy": "Claude Swap has too many active remote launches. Retry after one finishes.",
    "execution_failed": "Claude Swap could not start or relay the native remote process. Retry after checking the remote Mac's pool setup.",
}


class BridgeError(RuntimeError):
    def __init__(self, category="invalid_request"):
        self.category = category if category in MESSAGES else "execution_failed"
        super().__init__(MESSAGES[self.category])


def socket_path(home=None):
    return (DEFAULT_HOME if home is None else Path(home)) / ".config/claude-desktop-swap/gui-exec/socket"


def _json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def _frame(kind, data=b""):
    if len(data) > MAX_FRAME:
        raise BridgeError()
    return kind + struct.pack("!I", len(data)) + data


def _take(buffer):
    if len(buffer) < 5:
        return None
    size = struct.unpack("!I", buffer[1:5])[0]
    if size > MAX_FRAME:
        raise BridgeError()
    if len(buffer) < size + 5:
        return None
    kind, data = bytes(buffer[:1]), bytes(buffer[5:5 + size])
    del buffer[:5 + size]
    return kind, data


def peer_uid(conn):
    if hasattr(conn, "getpeereid"):
        return conn.getpeereid()[0]
    if sys.platform == "darwin":
        # CPython builds do not all expose getpeereid. The native BSD function
        # checks this accepted socket, without relying on claimed request fields.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if libc.getpeereid(conn.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise BridgeError()
        return uid.value
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    raise BridgeError()


def sanitized_environment(source, *, home=None):
    from .native import AUTH_ENV
    host_auth = source.get("CLAUDE_CODE_HOST_AUTH_ENV_VAR")
    blocked = set(AUTH_ENV) | {host_auth, "NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME",
        "LD_PRELOAD", "LD_LIBRARY_PATH", "GOOGLE_APPLICATION_CREDENTIALS"}
    result = {k: v for k, v in source.items() if k not in blocked and not k.startswith(
        ("ANTHROPIC_", "DYLD_", "AWS_", "AZURE_", "CLAUDE_CODE_OAUTH_", "CLAUDE_CODE_HOST_AUTH_"))}
    # The requester cannot move the GUI child into another native identity.
    identity = pwd.getpwuid(os.getuid())
    result["HOME"] = str(identity.pw_dir if home is None else home)
    result["USER"] = identity.pw_name
    result["LOGNAME"] = identity.pw_name
    result["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
    return result


def _owned_directory(path, uid, *, private=False):
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != uid or s.st_mode & (0o077 if private else 0o022):
        raise BridgeError()


def _runtime_directory(path, uid, *, create=False, home=None):
    boundary = DEFAULT_HOME if home is None else Path(home)
    if boundary.resolve() != boundary or not path.is_relative_to(boundary):
        raise BridgeError()
    _owned_directory(boundary, uid)
    current = boundary
    for part in path.relative_to(boundary).parts:
        current = current / part
        if create and not os.path.lexists(current):
            current.mkdir(mode=0o700)
        _owned_directory(current, uid, private=current == path)


def _socket_metadata(path, uid):
    s = path.lstat()
    if not stat.S_ISSOCK(s.st_mode) or s.st_uid != uid or s.st_mode & 0o077:
        raise BridgeError()
    return s


def validate_request(request, *, allowed_root, home, uid):
    if not isinstance(request, dict) or set(request) != {"argv", "cwd", "env"}:
        raise BridgeError()
    argv, cwd, env = (request[k] for k in ("argv", "cwd", "env"))
    if not isinstance(argv, list) or not 1 <= len(argv) <= 4096 or any(
        not isinstance(v, str) or "\0" in v or len(v.encode()) > 65536 for v in argv):
        raise BridgeError()
    executable = Path(argv[0])
    root = Path(allowed_root)
    if not executable.is_absolute() or executable.parent != root or executable.name in ("", ".", ".."):
        raise BridgeError()
    # No symlink escapes or writable ancestor permits replacing an approved CLI.
    current = root
    boundary = Path(home)
    if not root.is_relative_to(boundary):
        raise BridgeError()
    while True:
        _owned_directory(current, uid)
        if current == boundary:
            break
        current = current.parent
    s = executable.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != uid or s.st_mode & 0o022 or not s.st_mode & 0o100:
        raise BridgeError()
    if not isinstance(cwd, str) or "\0" in cwd or not Path(cwd).is_absolute():
        raise BridgeError()
    work = Path(cwd)
    _owned_directory(work, uid)
    if not os.access(work, os.R_OK | os.X_OK):
        raise BridgeError()
    if not isinstance(env, dict) or len(env) > 4096 or any(
        not isinstance(k, str) or not isinstance(v, str) or not k or "=" in k
        or "\0" in k or "\0" in v or len(k) > 1024 or len(v.encode()) > 65536
        for k, v in env.items()):
        raise BridgeError()
    return argv, cwd, sanitized_environment(env, home=home)


def check_pool():
    """Read local ownership/freshness only; never fetch or hand off a token.

    Security.framework interaction policy is process-wide. Serialize guards so
    overlapping requests cannot restore interactive access during another read.
    """
    if not _NATIVE_GUARD_LOCK.acquire(timeout=2):
        raise BridgeError("pool_unavailable")
    try:
        _check_pool()
    finally:
        _NATIVE_GUARD_LOCK.release()


def _check_pool():
    from .client import Client
    client = Client()
    try:
        state = client._load()
        expires = state.get("expires_at")
        if (state.get("state") not in {"active", "degraded"} or state.get("managed") is not True
            or "pending_write" in state or type(expires) not in (int, float)
            or not math.isfinite(expires) or expires <= client.clock() + 30):
            raise BridgeError("pool_unavailable")
        with client.native.noninteractive():
            client.native.preflight()
            snapshot = client.native.snapshot()
        if (not snapshot.get("has_credentials") or not state.get("fingerprint")
            or snapshot.get("fingerprint") != state["fingerprint"]):
            raise BridgeError("pool_unavailable")
        # An atomic state replacement during the native read must be retried.
        if client._load() != state:
            raise BridgeError("pool_unavailable")
    except Exception:
        raise BridgeError("pool_unavailable") from None


def _terminate_group(process):
    # Descendants may still own pipes after the CLI itself exits.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


class Server:
    """One bounded relay per connection; production uses fixed paths and guard."""
    def __init__(self, path=SOCKET, *, home=None, allowed_root=None, pool_check=check_pool,
                 peer_check=peer_uid):
        self.home = DEFAULT_HOME if home is None else Path(home)
        self.path = Path(path)
        self.root = self.home / ".claude/remote/ccd-cli" if allowed_root is None else Path(allowed_root)
        self.pool_check = pool_check
        self.peer_check = peer_check
        self.uid = os.getuid()
        self.stop_event = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.connections = set()
        self.workers = set()
        self.lock = threading.Lock()
        self.listener = None
        self.identity = None
        self.active_jobs = 0
        self.draining = False

    def bind(self):
        _runtime_directory(self.path.parent, self.uid, create=True, home=self.home)
        if os.path.lexists(self.path):
            _socket_metadata(self.path, self.uid)
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.settimeout(1)
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                self.path.unlink()  # An owned stale socket, never arbitrary files.
            else:
                raise BridgeError("execution_failed")
            finally:
                probe.close()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        self.identity = self.path.stat().st_ino
        self.listener.listen(MAX_CONNECTIONS)
        self.listener.settimeout(0.2)

    def serve_forever(self):
        if self.listener is None:
            self.bind()
        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                # Authenticate before reading even the first framing byte.
                try:
                    if self.peer_check(conn) != self.uid:
                        conn.close()
                        continue
                except Exception:
                    conn.close()
                    continue
                if not self.slots.acquire(blocking=False):
                    conn.close()
                    continue
                with self.lock:
                    self.connections.add(conn)
                worker = threading.Thread(target=self._handle, args=(conn,), daemon=True)
                with self.lock:
                    self.workers.add(worker)
                worker.start()
        finally:
            self.close()

    def close(self):
        self.stop_event.set()
        if self.listener:
            self.listener.close()
        with self.lock:
            for conn in self.connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        # launchd logout/stop must let connection cleanup kill owned process
        # groups before Python exits and discards its daemon relay threads.
        deadline = time.monotonic() + 3
        with self.lock:
            workers = tuple(self.workers)
        for worker in workers:
            if worker is not threading.current_thread() and worker.ident is not None:
                worker.join(timeout=max(0, deadline - time.monotonic()))
        try:
            if self.identity is not None and self.path.lstat().st_ino == self.identity:
                self.path.unlink()
        except FileNotFoundError:
            pass

    def _health(self, op, expected_pid=None):
        if op not in {"status", "prepare_stop", "resume", "check_ready"}:
            raise BridgeError()
        readiness = {}
        if op == "check_ready":
            try:
                self.pool_check()
                readiness = {"native_ready": True, "native_category": "ready"}
            except Exception:
                readiness = {"native_ready": False, "native_category": "pool_unavailable"}
        with self.lock:
            if op in {"prepare_stop", "resume"}:
                if type(expected_pid) is not int or expected_pid != os.getpid():
                    raise BridgeError()
            elif expected_pid is not None:
                raise BridgeError()
            ok = True
            if op == "prepare_stop":
                if self.active_jobs:
                    ok = False
                else:
                    self.draining = True
            elif op == "resume":
                self.draining = False
            return {"ok": ok, "active_jobs": self.active_jobs, "draining": self.draining,
                    "pid": os.getpid(), "uid": self.uid, "python": sys.executable,
                    "module": "team_pool.gui_exec", "protocol_version": PROTOCOL_VERSION,
                    "revision": GUI_EXEC_REVISION, **readiness}

    def _handle(self, conn):
        process = None
        registered = False
        try:
            conn.settimeout(5)
            incoming = bytearray()
            while (frame := _take(incoming)) is None:
                data = conn.recv(CHUNK)
                if not data:
                    raise BridgeError()
                incoming.extend(data)
            kind, payload = frame
            if incoming:
                raise BridgeError()
            if kind == b"H":
                query = json.loads(payload)
                if not isinstance(query, dict) or set(query) not in ({"op"}, {"op", "expected_pid"}):
                    raise BridgeError()
                conn.sendall(_frame(b"H", _json(self._health(query["op"], query.get("expected_pid")))))
                return
            if kind != b"J":
                raise BridgeError()
            request = json.loads(payload)
            argv, cwd, env = validate_request(request, allowed_root=self.root, home=self.home, uid=self.uid)
            with self.lock:
                if self.draining or self.active_jobs >= MAX_JOBS:
                    raise BridgeError("busy")
                self.active_jobs += 1
                registered = True
            self.pool_check()
            if self.stop_event.is_set():
                raise BridgeError("gui_required")
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
            self._pump(conn, process)
        except Exception as exc:
            category = exc.category if isinstance(exc, BridgeError) else "execution_failed"
            try:
                conn.settimeout(0.2)
                conn.sendall(_frame(b"F", category.encode()))
            except OSError:
                pass
        finally:
            if process:
                _terminate_group(process)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if not pipe.closed:
                        pipe.close()
            conn.close()
            with self.lock:
                self.connections.discard(conn)
                self.workers.discard(threading.current_thread())
                if registered:
                    self.active_jobs -= 1
            self.slots.release()

    def _pump(self, conn, process):
        conn.setblocking(False)
        pipes = {process.stdout.fileno(): b"O", process.stderr.fileno(): b"E"}
        for pipe in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(pipe.fileno(), False)
        incoming, outgoing, stdin = bytearray(), bytearray(_frame(b"R")), bytearray()
        eof, exited, exit_sent = False, None, False
        while True:
            status = process.poll()
            if status is not None and exited is None:
                exited = time.monotonic()
            if exited is not None and time.monotonic() - exited > 0.5 and pipes:
                _terminate_group(process)
            if eof and not stdin and not process.stdin.closed:
                process.stdin.close()
            if status is not None and not pipes and not exit_sent:
                outgoing.extend(_frame(b"X", str(status if status >= 0 else 128 - status).encode()))
                exit_sent = True
            if exit_sent and not outgoing:
                return
            reads = [conn]
            if len(outgoing) < MAX_OUTPUT:
                reads.extend(pipes)
            writes = [conn] if outgoing else []
            if stdin and not process.stdin.closed:
                writes.append(process.stdin.fileno())
            ready_r, ready_w, _ = select.select(reads, writes, [], 0.2)
            if conn in ready_r:
                data = conn.recv(CHUNK)
                if not data:
                    return
                incoming.extend(data)
                while (frame := _take(incoming)) is not None:
                    kind, payload = frame
                    if kind == b"I" and not eof and 0 < len(payload) <= CHUNK and len(stdin) + len(payload) <= WINDOW:
                        stdin.extend(payload)
                    elif kind == b"C" and not payload and not eof:
                        eof = True
                    elif kind == b"S" and len(payload) == 1 and payload[0] in SIGNALS:
                        try:
                            os.killpg(process.pid, payload[0])
                        except ProcessLookupError:
                            pass
                    else:
                        raise BridgeError()
            for fd in list(pipes):
                if fd in ready_r:
                    data = os.read(fd, CHUNK)
                    if data:
                        outgoing.extend(_frame(pipes[fd], data))
                    else:
                        del pipes[fd]
            if not process.stdin.closed and process.stdin.fileno() in ready_w:
                try:
                    count = os.write(process.stdin.fileno(), stdin)
                    del stdin[:count]
                    outgoing.extend(_frame(b"W", struct.pack("!I", count)))
                except BrokenPipeError:
                    stdin.clear()
                    process.stdin.close()
            if conn in ready_w:
                count = conn.send(outgoing)
                del outgoing[:count]


def run(argv, *, path=SOCKET, home=None, stdin_fd=0, stdout_fd=1, stderr_fd=2,
        environment=None, install_signals=True):
    """Relay bytes; stdin EOF is separate from cancellation/disconnection."""
    conn = socket.socket(socket.AF_UNIX)
    handlers, blocking = {}, {}
    signals = deque(maxlen=16)
    try:
        _runtime_directory(Path(path).parent, os.getuid(), home=home)
        _socket_metadata(Path(path), os.getuid())
        conn.settimeout(5)
        conn.connect(str(path))
        if peer_uid(conn) != os.getuid():
            raise BridgeError()
        request = {"argv": argv, "cwd": os.getcwd(), "env": sanitized_environment(
            dict(os.environ if environment is None else environment), home=home)}
        conn.sendall(_frame(b"J", _json(request)))
        conn.setblocking(False)
        for fd in (stdin_fd, stdout_fd, stderr_fd):
            blocking[fd] = os.get_blocking(fd)
            os.set_blocking(fd, False)
        if install_signals:
            for sig in SIGNALS:
                handlers[sig] = signal.signal(sig, lambda number, frame: signals.append(number))
        incoming, outgoing = bytearray(), bytearray()
        output = {stdout_fd: bytearray(), stderr_fd: bytearray()}
        credit, ready, eof, result = WINDOW, False, False, None
        while True:
            while signals:
                number = signals.popleft()
                if result is None:
                    outgoing.extend(_frame(b"S", bytes([number])))
            if result is not None and not any(output.values()):
                return result
            reads = []
            if result is None and all(len(v) < MAX_OUTPUT for v in output.values()):
                reads.append(conn)
            if ready and not eof and credit and len(outgoing) < WINDOW:
                reads.append(stdin_fd)
            writes = [fd for fd, value in output.items() if value]
            if outgoing:
                writes.append(conn)
            readable, writable, _ = select.select(reads, writes, [], 0.2)
            if conn in readable:
                data = conn.recv(CHUNK)
                if not data:
                    raise BridgeError("execution_failed")
                incoming.extend(data)
                while (frame := _take(incoming)) is not None:
                    kind, payload = frame
                    if kind in (b"O", b"E") and len(payload) <= CHUNK:
                        output[stdout_fd if kind == b"O" else stderr_fd].extend(payload)
                    elif kind == b"R" and not payload and not ready:
                        ready = True
                    elif kind == b"W" and len(payload) == 4:
                        credit += struct.unpack("!I", payload)[0]
                        if credit > WINDOW:
                            raise BridgeError()
                    elif kind == b"X" and payload.isdigit() and 0 <= int(payload) <= 255:
                        result = int(payload)
                        outgoing.clear()
                        eof = True
                    elif kind == b"F":
                        raise BridgeError(payload.decode("ascii", errors="replace"))
                    else:
                        raise BridgeError()
            if stdin_fd in readable and result is None:
                data = os.read(stdin_fd, min(CHUNK, credit))
                if data:
                    credit -= len(data)
                    outgoing.extend(_frame(b"I", data))
                else:
                    outgoing.extend(_frame(b"C"))
                    eof = True
            if conn in writable and outgoing:
                count = conn.send(outgoing)
                del outgoing[:count]
            for fd, value in output.items():
                if fd in writable and value:
                    count = os.write(fd, value)
                    del value[:count]
    except (FileNotFoundError, ConnectionRefusedError):
        raise BridgeError("gui_required") from None
    except BridgeError:
        raise
    except Exception:
        raise BridgeError("execution_failed") from None
    finally:
        conn.close()
        for sig, previous in handlers.items():
            signal.signal(sig, previous)
        for fd, previous in blocking.items():
            try:
                os.set_blocking(fd, previous)
            except OSError:
                pass


def health(socket_path=SOCKET, *, timeout=3, op="status", home=None, expected_pid=None):
    """Same-UID metadata probe; prepare_stop atomically drains an idle server."""
    conn = socket.socket(socket.AF_UNIX)
    try:
        _runtime_directory(Path(socket_path).parent, os.getuid(), home=home)
        _socket_metadata(Path(socket_path), os.getuid())
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        if peer_uid(conn) != os.getuid():
            raise BridgeError()
        query = {"op": op}
        if op in {"prepare_stop", "resume"}:
            if type(expected_pid) is not int or expected_pid <= 0:
                raise BridgeError()
            query["expected_pid"] = expected_pid
        elif expected_pid is not None:
            raise BridgeError()
        conn.sendall(_frame(b"H", _json(query)))
        incoming = bytearray()
        while (frame := _take(incoming)) is None:
            data = conn.recv(CHUNK)
            if not data:
                raise BridgeError("execution_failed")
            incoming.extend(data)
        kind, payload = frame
        if kind != b"H":
            raise BridgeError("execution_failed")
        value = json.loads(payload)
        required = {"ok", "active_jobs", "draining", "pid", "uid", "python", "module", "protocol_version", "revision"}
        allowed = required | {"native_ready", "native_category"}
        if (not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= allowed
            or value.get("module") != "team_pool.gui_exec" or type(value.get("ok")) is not bool
            or type(value.get("active_jobs")) is not int or value["active_jobs"] < 0
            or type(value.get("uid")) is not int or value["uid"] != os.getuid()
            or type(value.get("pid")) is not int or value["pid"] <= 0
            or not isinstance(value.get("python"), str) or len(value["python"]) > 4096
            or not Path(value["python"]).is_absolute()
            or type(value.get("protocol_version")) is not int
            or value["protocol_version"] != PROTOCOL_VERSION
            or not isinstance(value.get("revision"), str) or len(value["revision"]) > 64
            or type(value.get("draining")) is not bool):
            raise BridgeError("execution_failed")
        if op == "check_ready" and (type(value.get("native_ready")) is not bool
            or value.get("native_category") not in {"ready", "pool_unavailable"}):
            raise BridgeError("execution_failed")
        return value
    except BridgeError:
        raise
    except Exception:
        raise BridgeError("gui_required") from None
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "run"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.mode == "run":
            command = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            if not command:
                raise BridgeError()
            return run(command)
        if args.arguments or sys.platform != "darwin":
            raise BridgeError("gui_required")
        context = subprocess.run(["/bin/launchctl", "managername"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False)
        if context.returncode or context.stdout.strip() != b"Aqua":
            raise BridgeError("gui_required")
        # Sanitize the daemon's own native preflight environment as well.
        clean = sanitized_environment(dict(os.environ))
        os.environ.clear()
        os.environ.update(clean)
        server = Server()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: server.stop_event.set())
        server.serve_forever()
        return 0
    except Exception as exc:
        category = exc.category if isinstance(exc, BridgeError) else "execution_failed"
        print(MESSAGES[category], file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
