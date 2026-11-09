"""Lifecycle of the same-user GUI execution agent, without native credentials.

The agent owns no Desktop helper or conversation. Replacement/removal requires
an authenticated, atomic drain so a concurrent CLI request cannot be killed by
an installer that observed an earlier idle snapshot.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time

from .client import ClientError


SERVICE = "study.paradigm.claude-desktop-swap.gui-exec"
_REVISION_KEY = "ClaudeDesktopSwapGUIExecRevision"
_MAX_FILE = 64 * 1024


class GuiServiceError(ClientError):
    """Fixed, safe-to-display lifecycle error; never includes subprocess output."""


def service_path(home=None):
    return Path(home or Path.home()) / "Library/LaunchAgents" / (SERVICE + ".plist")


def runtime_path(home=None):
    return Path(home or Path.home()) / ".config/claude-desktop-swap/gui-exec"


def _platform(platform):
    if (sys.platform if platform is None else platform) != "darwin":
        raise GuiServiceError("GUI execution installation requires macOS.")
    if os.geteuid() == 0:
        raise GuiServiceError("Run GUI execution setup as the enrolled macOS user, without sudo.")


def _owned(info, *, kind, private=False):
    return (kind(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & (0o077 if private else 0o022))


def _directory(path, *, private=False, create=False):
    try:
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not _owned(info, kind=stat.S_ISDIR, private=private):
            raise ValueError
        if private and stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError
    except (OSError, ValueError):
        raise GuiServiceError("GUI execution directories must be owned, safe directories; the runtime must have mode 0700.") from None


def _paths(home, *, create):
    home = Path(os.path.abspath(home or Path.home()))
    _directory(home)
    for relative in (".config", ".config/claude-desktop-swap", "Library", "Library/LaunchAgents"):
        _directory(home / relative, create=create)
    runtime = runtime_path(home)
    _directory(runtime, private=True, create=create)
    return home, service_path(home), runtime


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not _owned(info, kind=stat.S_ISREG, private=True) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError
            raw = stream.read(_MAX_FILE + 1)
            if len(raw) > _MAX_FILE:
                raise ValueError
            return raw
    except (OSError, ValueError):
        raise GuiServiceError("Refusing an unsafe GUI execution service file; files must be owned and have mode 0600.") from None


def _exists(path):
    return path.exists() or path.is_symlink()


def _write(path, raw):
    if _exists(path):
        _read(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".gui-exec-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise GuiServiceError("Cannot securely write the GUI execution service file.") from None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _log(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not _owned(info, kind=stat.S_ISREG, private=True) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise GuiServiceError("GUI execution logs must be owned regular files with mode 0600.") from None


@contextmanager
def _lock(runtime):
    fd = None
    try:
        fd = os.open(runtime / "lifecycle.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not _owned(info, kind=stat.S_ISREG, private=True) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError):
        if fd is not None:
            os.close(fd)
        raise GuiServiceError("GUI execution setup is busy or its lifecycle lock is unsafe.") from None
    try:
        yield
    finally:
        os.close(fd)


def _runtime_python(value):
    # Preserve the lexical venv path: resolving its symlink selects the global
    # interpreter and silently loses the installed package/environment.
    if not isinstance(value, str) or not os.path.isabs(value) or os.path.abspath(value) != value or any(c in value for c in "\n\r\0"):
        raise GuiServiceError("GUI execution requires an absolute installed Python runtime.")
    try:
        info = os.stat(value)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022 or not os.access(value, os.X_OK):
            raise ValueError
    except (OSError, ValueError):
        raise GuiServiceError("GUI execution requires a trusted executable Python runtime.") from None
    return value


def _spec(home, python, working, revision):
    log = str(runtime_path(home) / "service.log")
    return {
        "Label": SERVICE,
        "ProgramArguments": [python, "-m", "team_pool.gui_exec", "serve"],
        "WorkingDirectory": working,
        "RunAtLoad": True, "KeepAlive": True,
        "LimitLoadToSessionType": "Aqua", "ThrottleInterval": 10,
        "StandardOutPath": log, "StandardErrorPath": log, "Umask": 0o077,
        "EnvironmentVariables": {"PYTHONNOUSERSITE": "1"},
        _REVISION_KEY: revision,
    }


def _owned_spec(raw, home):
    try:
        spec = plistlib.loads(raw)
        args = spec["ProgramArguments"]
        working = spec["WorkingDirectory"]
        revision = spec[_REVISION_KEY]
        if (not isinstance(args, list) or len(args) != 4 or not isinstance(working, str)
                or not os.path.isabs(working) or os.path.abspath(working) != working
                or any(c in working for c in "\n\r\0") or not isinstance(revision, str)
                or not re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", revision)
                or spec != _spec(home, args[0], working, revision)):
            raise ValueError
        if (any(type(spec[key]) is not bool for key in ("RunAtLoad", "KeepAlive"))
                or any(type(spec[key]) is not int for key in ("Umask", "ThrottleInterval"))):
            raise ValueError
        _runtime_python(args[0])
        info = Path(working).stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise ValueError
        return spec
    except (OSError, ValueError, TypeError, KeyError, plistlib.InvalidFileException):
        raise GuiServiceError("Refusing to change an unrecognized GUI execution LaunchAgent.") from None


def _run(runner, args):
    try:
        value = runner(["/bin/launchctl", *args], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=3, check=False)
        if not isinstance(value.stdout, (str, bytes)) or len(value.stdout) > _MAX_FILE:
            raise ValueError
        return value
    except (OSError, ValueError, subprocess.SubprocessError):
        raise GuiServiceError("Could not inspect or control this user's GUI execution LaunchAgent.") from None


def _loaded(runner):
    value = _run(runner, ["print", f"gui/{os.getuid()}/{SERVICE}"])
    if value.returncode in (112, 113):
        return None
    if value.returncode:
        raise GuiServiceError("Could not inspect this user's GUI execution LaunchAgent.")
    raw = value.stdout.decode("utf-8", errors="replace") if isinstance(value.stdout, bytes) else value.stdout
    result = {}
    for field in ("path", "program", "working directory", "state", "type", "pid"):
        matches = re.findall(r"^\t" + re.escape(field) + r" = ([^\r\n]+)$", raw, re.MULTILINE)
        if len(matches) == 1:
            result[field] = matches[0]
    matches = re.findall(r"^\targuments = \{\n((?:\t\t[^\r\n]+\n)+)\t\}$", raw, re.MULTILINE)
    if len(matches) == 1:
        result["arguments"] = [line[2:] for line in matches[0].splitlines()]
    return result


def _validate_loaded(value, spec, destination):
    if (value.get("path") != str(destination) or value.get("type") != "LaunchAgent"
            or value.get("program") != spec["ProgramArguments"][0]
            or value.get("arguments") != spec["ProgramArguments"]
            or value.get("working directory") != spec["WorkingDirectory"]):
        raise GuiServiceError("Refusing to control a GUI execution process with an unrecognized path or runtime.")


def _health(health, runtime, spec, loaded, *, op="status", timeout=1):
    try:
        expected = {"expected_pid": int(loaded["pid"])} if op in ("prepare_stop", "resume") else {}
        value = health(runtime / "socket", timeout=timeout, op=op, **expected)
    except Exception:
        return None
    if (not isinstance(value, dict) or type(value.get("protocol_version")) is not int or value["protocol_version"] != 1
            or value.get("revision") != spec[_REVISION_KEY]
            or value.get("module") != "team_pool.gui_exec"
            or value.get("python") != spec["ProgramArguments"][0]
            or type(value.get("uid")) is not int or value["uid"] != os.getuid()
            or type(value.get("pid")) is not int or value["pid"] <= 0
            or str(value["pid"]) != loaded.get("pid")
            or type(value.get("active_jobs")) is not int or value["active_jobs"] < 0
            or type(value.get("draining")) is not bool or type(value.get("ok")) is not bool
            or loaded.get("state") != "running"):
        return None
    return value


def _wait_ready(runner, health, runtime, spec, destination, timeout, clock, sleep):
    deadline = clock() + timeout
    while True:
        loaded = _loaded(runner)
        if loaded is not None:
            _validate_loaded(loaded, spec, destination)
            value = _health(health, runtime, spec, loaded, op="check_ready", timeout=min(3, max(0.05, deadline - clock())))
            if value and value["ok"] and not value["draining"] and value.get("native_ready") is True:
                return value
        remaining = deadline - clock()
        if remaining <= 0:
            raise GuiServiceError("GUI execution did not become ready before the setup deadline.")
        sleep(min(0.1, remaining))


def _stop(runner, health, runtime, spec, destination):
    loaded = _loaded(runner)
    if loaded is None:
        return
    _validate_loaded(loaded, spec, destination)
    # Check the service's identity before sending any state-changing request.
    # prepare_stop then rechecks idleness under the server's job-registration
    # lock, closing the race between this status read and a new request.
    current_health = _health(health, runtime, spec, loaded)
    if current_health and current_health["active_jobs"]:
        raise GuiServiceError("GUI execution has active Claude sessions. Finish those sessions before updating or removing it.")
    if not current_health or not current_health["ok"]:
        raise GuiServiceError("GUI execution could not safely stop accepting work; no running jobs were interrupted.")
    try:
        value = _health(health, runtime, spec, loaded, op="prepare_stop")
        if value and value["active_jobs"]:
            raise GuiServiceError("GUI execution has active Claude sessions. Finish those sessions before updating or removing it.")
        if not value or not value["ok"] or not value["draining"]:
            raise GuiServiceError("GUI execution could not safely stop accepting work; no running jobs were interrupted.")
        current = _loaded(runner)
        if current is None:
            return
        _validate_loaded(current, spec, destination)
        if current.get("pid") != loaded.get("pid"):
            raise GuiServiceError("GUI execution changed during setup; retry after checking its service.")
        stopped = _run(runner, ["bootout", f"gui/{os.getuid()}/{SERVICE}"])
        if stopped.returncode or _loaded(runner) is not None:
            raise GuiServiceError("GUI execution could not be stopped safely.")
    except GuiServiceError:
        # prepare_stop may have succeeded even if its response was lost. The
        # PID guard makes this safe even if launchd replaced the server since.
        _health(health, runtime, spec, loaded, op="resume")
        raise


def _bootstrap(runner, destination):
    value = _run(runner, ["bootstrap", f"gui/{os.getuid()}", str(destination)])
    if value.returncode:
        raise GuiServiceError("GUI execution could not start in this user's macOS login session.")


def install_service(*, home=None, runner=subprocess.run, python=None, health=None,
                    timeout=10, clock=time.monotonic, sleep=time.sleep, platform=None):
    """Install/upgrade the agent after the caller has established an active pool.

    A healthy identical installation is left running, including its active jobs.
    Errors never force-stop an unresponsive process or discard rollback state.
    """
    _platform(platform)
    from .gui_exec import GUI_EXEC_REVISION, health as default_health
    health = health or default_health
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise GuiServiceError("GUI execution readiness timeout must be between zero and 60 seconds.")
    home, destination, runtime = _paths(home, create=True)
    python = _runtime_python(os.path.abspath(python or sys.executable))
    specification = _spec(home, python, os.path.abspath(Path(__file__).parent.parent), GUI_EXEC_REVISION)
    new_raw = plistlib.dumps(specification)
    with _lock(runtime):
        previous_raw = _read(destination) if _exists(destination) else None
        previous = _owned_spec(previous_raw, home) if previous_raw is not None else None
        loaded = _loaded(runner)
        if loaded is not None:
            if previous is None:
                raise GuiServiceError("An unowned GUI execution service is already loaded; setup did not change it.")
            _validate_loaded(loaded, previous, destination)
            if previous == specification:
                value = _health(health, runtime, previous, loaded, op="check_ready", timeout=3)
                if value and value["ok"] and not value["draining"] and value.get("native_ready") is True:
                    _log(runtime / "service.log")
                    return {"status": "unchanged", "pid": value["pid"]}
        _log(runtime / "service.log")
        backup = None
        if previous_raw is not None:
            fd, name = tempfile.mkstemp(prefix="rollback-", suffix=".plist", dir=runtime)
            backup = Path(name)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(previous_raw)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            if loaded is not None:
                _stop(runner, health, runtime, previous, destination)
        except GuiServiceError:
            if backup is not None:
                backup.unlink()
            raise
        new_written = False
        try:
            _write(destination, new_raw)
            new_written = True
            _bootstrap(runner, destination)
            value = _wait_ready(runner, health, runtime, specification, destination, timeout, clock, sleep)
        except GuiServiceError as error:
            try:
                if new_written:
                    _stop(runner, health, runtime, specification, destination)
                    if _read(destination) != new_raw:
                        raise GuiServiceError("GUI execution service file changed during setup.")
                elif (_read(destination) if _exists(destination) else None) != previous_raw:
                    raise GuiServiceError("GUI execution service file changed during setup.")
                if previous_raw is None:
                    if new_written:
                        destination.unlink()
                else:
                    _write(destination, previous_raw)
                    if loaded is not None:
                        _bootstrap(runner, destination)
                        _wait_ready(runner, health, runtime, previous, destination, timeout, clock, sleep)
            except (GuiServiceError, OSError):
                raise GuiServiceError("GUI execution setup could not finish or safely roll back. Recovery files were retained; no active jobs were force-stopped.") from None
            if backup is not None:
                backup.unlink()
            raise error
        if backup is not None:
            backup.unlink()
        return {"status": "updated" if previous is not None else "installed", "pid": value["pid"]}


def remove_service(*, home=None, runner=subprocess.run, health=None, platform=None):
    """Remove only this owned agent and only when it atomically reports idle."""
    _platform(platform)
    destination = service_path(Path(os.path.abspath(home or Path.home())))
    if not _exists(destination):
        if _loaded(runner) is not None:
            raise GuiServiceError("An unowned GUI execution service is loaded; removal did not change it.")
        return {"status": "absent"}
    from .gui_exec import health as default_health
    health = health or default_health
    home, destination, runtime = _paths(home, create=False)
    with _lock(runtime):
        raw = _read(destination)
        specification = _owned_spec(raw, home)
        _stop(runner, health, runtime, specification, destination)
        if _read(destination) != raw:
            raise GuiServiceError("GUI execution service file changed during removal; it was retained.")
        destination.unlink()
    return {"status": "removed"}
