"""Install the native shared pool using this Mac's existing Claude Share login."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from .client import (Client, ClientError, SERVICE, _lock, _private_read, _private_write,
                     install_service, service_path, verify_desktop_wrapper)
from .native import NativeKeychainAccessError, NativePolicyError, NativeStoreError
from .share_enrollment import (DEFAULT_ACCOUNT_FILE, DEFAULT_SHARE_SERVER,
                               ShareTransport, enroll_from_share, saved_token)

TEAM_ID = "B-pTJOWkq5wl"
KEYCHAIN_GUIDANCE = ('If macOS asks for “security” to access “Claude Code-credentials”, '
                     'choose Always Allow for that exact request. Never paste a password or token here.')


@contextmanager
def setup_lock(path):
    """A separate lock allows client operations to retain their existing lock."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o022:
        raise ClientError("Setup directory must be owned by you and not writable by other users.")
    fd = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ClientError("Unsafe setup lock; preserve it and check its ownership and permissions.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ClientError("Another setup is running. Let it finish before trying again.") from None
        yield
    finally:
        if fd is not None:
            os.close(fd)


def share_membership(account_file, *, transport=None):
    """Check authoritative membership without issuing a short-lived grant."""
    bearer = saved_token(account_file, DEFAULT_SHARE_SERVER)
    transport = transport or ShareTransport(DEFAULT_SHARE_SERVER)
    request = urllib.request.Request(DEFAULT_SHARE_SERVER + "/api/me", headers={
        "Authorization": "Bearer " + bearer, "Accept": "application/json",
        "User-Agent": "claude-desktop-swap-setup/1"})
    try:
        with transport.opener.open(request, timeout=20) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError
        value = json.loads(raw)
        identity = value["account"]["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", identity):
            raise ValueError
        teams = value["teams"]
        if not isinstance(teams, list):
            raise ValueError
        if not any(isinstance(team, dict) and team.get("id") == TEAM_ID
                   and team.get("role") in ("owner", "member") for team in teams):
            raise ClientError("Your Claude Share account must be a member of Paradigm. Open /team and join Paradigm before setup.")
        return identity
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code == 401:
            raise ClientError("Sign in again to Claude Share with /team, then repeat setup.") from None
        raise ClientError("Claude Share membership could not be verified. Repeat setup when the server is available.") from None
    except ClientError:
        raise
    except Exception:
        raise ClientError("Claude Share membership could not be verified. Check connectivity and repeat setup.") from None


def inspect_state(client):
    """Read-only: never turn a manual login or interrupted write into takeover."""
    state = client._load(optional=True)
    if state:
        if state.get("pending_write"):
            raise ClientError("A native write needs recovery. Run team-pool-client status and follow the recovery guide; setup will not overwrite it.")
        if state.get("state") in ("paused", "revoked", "left"):
            raise ClientError("The pool client is paused, revoked, or left. Run team-pool-client status and resolve that condition explicitly before setup.")
        if state.get("state") not in ("enrolled", "active", "degraded"):
            raise ClientError("Unrecognized pool state; preserve it for recovery before setup.")
    try:
        client.native.preflight()
        snapshot = client.native.snapshot()
    except NativeKeychainAccessError:
        raise ClientError("Unlock your login Keychain and run setup in Terminal to approve the expected access request. Readiness checks never request approval.") from None
    except NativePolicyError:
        raise ClientError("Native subscription settings contain an unsupported provider, login policy, or credential-file override. Resolve that configuration before setup; no native credentials were changed.") from None
    except NativeStoreError:
        raise ClientError("Cannot inspect the native credential store or recovery backup. Check Keychain access and preserve existing recovery files before repeating setup.") from None
    if state and state.get("managed"):
        if snapshot["fingerprint"] != state.get("fingerprint"):
            raise ClientError("Native credentials changed outside the pool. Setup will not overwrite a manual login; inspect team-pool-client status and the recovery guide.")
        if not snapshot["has_backup"]:
            raise ClientError("The native recovery backup is missing. Preserve the existing enrollment and request recovery help.")
    elif snapshot["has_backup"]:
        raise ClientError("A native recovery backup already exists without an active owner. Resolve recovery before setup; the backup was preserved.")
    return state


class Setup:
    def __init__(self, source_dir, *, client=None, account_file=DEFAULT_ACCOUNT_FILE,
                 home=None, runner=subprocess.run, sleep=time.sleep, clock=time.monotonic,
                 membership=share_membership, enroll=enroll_from_share,
                 installer=install_service, verify_wrapper=verify_desktop_wrapper,
                 output=print):
        self.source = Path(source_dir).expanduser()
        self.home = Path(home or Path.home())
        self.client = client or Client()
        self.account_file = Path(account_file).expanduser()
        self.runner, self.sleep, self.clock = runner, sleep, clock
        self.membership, self.enroll, self.installer = membership, enroll, installer
        self.verify_wrapper, self.output = verify_wrapper, output
        self.uid = os.getuid()
        self.domain = f"gui/{self.uid}"
        self.service = self.domain + "/" + SERVICE
        self.personal = self.domain + "/com.cswap.menubar"

    def command(self, args, *, visible=False, timeout=30):
        try:
            return self.runner(args, check=False, text=True, timeout=timeout,
                               **({} if visible else {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}))
        except subprocess.TimeoutExpired:
            raise ClientError("A setup command timed out. Existing recovery state was retained; close any blocked dialog and repeat setup.") from None
        except OSError:
            raise ClientError("A required setup command could not run. Check the Mac prerequisites and repeat setup.") from None

    def loaded(self, target):
        result = self.command(["/bin/launchctl", "print", target])
        if result.returncode == 0:
            return result.stdout or ""
        if result.returncode in (3, 113) or "Could not find service" in (result.stderr or ""):
            return None
        raise ClientError("Cannot determine whether the background service is running. No service was replaced.")

    def manual_selector(self):
        result = self.command(["/bin/ps", "-axo", "pid=,uid=,command="])
        if result.returncode:
            raise ClientError("Cannot inspect account selectors; resolve process access before setup.")
        for line in (result.stdout or "").splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3 and fields[1] == str(self.uid) and re.search(r"(?:^|[/\s])cswap\s+auto(?:\s|$)", fields[2]):
                raise ClientError("A manual cswap auto process is running. Stop it in its Terminal, then repeat setup.")

    def prerequisites(self):
        if sys.platform != "darwin" or os.geteuid() == 0:
            raise ClientError("Run setup in Terminal as your ordinary macOS user, without sudo.")
        if not self.source.is_absolute():
            raise ClientError("Use an absolute release source directory.")
        for relative in ("scripts/desktop_swap.py", "scripts/install_team_status.py"):
            item = self.source / relative
            if not item.is_file() or item.is_symlink():
                raise ClientError("The installed release is incomplete. Repeat the one-command installer.")
        self.manual_selector()

    def service_spec(self, loaded):
        path = service_path(self.home)
        if not path.exists() and not path.is_symlink():
            if loaded is not None:
                raise ClientError("An unrecognized team service is loaded without its service file. Preserve it and inspect launchctl before setup.")
            return None
        try:
            value = plistlib.loads(_private_read(path))
            args = value["ProgramArguments"]
            if (value.get("Label") != SERVICE or not isinstance(args, list) or len(args) != 6
                    or not isinstance(args[0], str) or not Path(args[0]).is_absolute()
                    or args[1:] != ["-m", "team_pool.client", "--config", str(self.client.config), "run"]):
                raise ValueError
            if loaded is not None:
                match = re.search(r"(?m)^\s*path = (.+)$", loaded)
                if not match or match.group(1).strip() != str(path):
                    raise ValueError
            return path
        except (KeyError, TypeError, ValueError, plistlib.InvalidFileException):
            raise ClientError("Refusing to replace an unrecognized team service; inspect its saved configuration.") from None

    def wait_stopped(self, target, before):
        pid = re.search(r"(?m)^\s*pid = (\d+)$", before or "")
        deadline = self.clock() + 15
        while self.clock() < deadline:
            gone = self.loaded(target) is None
            process_gone = True
            if pid:
                running = self.command(["/bin/ps", "-p", pid.group(1), "-o", "command="])
                process_gone = running.returncode == 1
                if running.returncode not in (0, 1):
                    raise ClientError("Cannot verify that the previous sync process exited. Its service file was preserved.")
            if gone and process_gone:
                return
            self.sleep(0.2)
        raise ClientError("The previous background service did not stop. Its service file was preserved; inspect it before repeating setup.")

    def stop_team(self):
        loaded = self.loaded(self.service)
        path = self.service_spec(loaded)
        if loaded is not None:
            result = self.command(["/bin/launchctl", "bootout", self.service])
            if result.returncode:
                raise ClientError("The background team service could not stop. Its service file was preserved; retry after it exits.")
            self.wait_stopped(self.service, loaded)
        return path

    def stop_personal(self):
        loaded = self.loaded(self.personal)
        result = self.command(["/bin/launchctl", "disable", self.personal])
        if result.returncode:
            raise ClientError("Could not disable the personal cswap selector. Stop it explicitly before joining the shared pool.")
        if loaded is not None:
            result = self.command(["/bin/launchctl", "bootout", self.personal])
            if result.returncode:
                raise ClientError("Could not stop the personal cswap selector; setup did not take over native credentials.")
            self.wait_stopped(self.personal, loaded)
        self.manual_selector()

    def desktop_running(self):
        result = self.command(["/usr/bin/pgrep", "-u", str(self.uid), "-x", "Claude"])
        if result.returncode not in (0, 1):
            raise ClientError("Could not check whether Claude Desktop is running; quit it manually and retry.")
        return result.returncode == 0

    def install_wrapper(self):
        was_running = self.desktop_running()
        if was_running:
            result = self.command(["/usr/bin/osascript", "-e", 'tell application id "com.anthropic.claudefordesktop" to quit'], timeout=35)
            if result.returncode:
                raise ClientError("Claude Desktop could not quit. Save any work, quit it yourself, then repeat setup.")
            deadline = self.clock() + 20
            while self.desktop_running() and self.clock() < deadline:
                self.sleep(0.25)
            if self.desktop_running():
                raise ClientError("Claude Desktop is still open. Finish any confirmation dialog and quit it, then repeat setup.")
        # Keep this flag even if sudo or wrapper verification fails, so a Mac
        # whose Desktop was closed by setup is reopened in the outer finally.
        self.reopen_desktop = was_running
        result = self.command(["/usr/bin/sudo", sys.executable,
                               str(self.source / "scripts/desktop_swap.py"), "install", "--user",
                               pwd.getpwuid(self.uid).pw_name], visible=True, timeout=180)
        if result.returncode:
            raise ClientError("Native Desktop launcher installation did not complete. Existing recovery state was retained; repeat setup after resolving the installer error.")
        self.verify_wrapper()
        return was_running

    def background_ready(self, foreground_synced_at):
        deadline = self.clock() + 80
        saw_process = False
        while self.clock() < deadline:
            state = self.client._load()
            if state.get("state") in ("paused", "revoked", "left") or state.get("pending_write"):
                # pending_write may be a normal in-flight commit; only persistent
                # recovery is rejected after the process settles below.
                if state.get("state") in ("paused", "revoked", "left"):
                    raise ClientError("Background sync stopped or paused. Run team-pool-client status and correct the reported condition; setup is not complete.")
            loaded = self.loaded(self.service)
            running = loaded is not None and bool(re.search(r"(?m)^\s*pid = \d+$", loaded))
            exited = loaded is not None and re.search(r"(?m)^\s*last exit code = \d+$", loaded)
            if loaded is None or (not running and (saw_process or exited)):
                raise ClientError("The background sync process exited. Run team-pool-client status and inspect its private agent.log; setup is not complete.")
            saw_process = saw_process or running
            if (running and state.get("synced_at", 0) > foreground_synced_at and state.get("state") == "active"
                    and not state.get("pending_write") and state.get("managed")):
                return
            self.sleep(1)
        raise ClientError("Background sync did not confirm an active assignment in time. Run team-pool-client status; setup is not complete.")

    def run(self, *, check=False):
        self.prerequisites()
        if check:
            return self._run(check=True)
        with setup_lock(self.client.config.parent / "setup.lock"):
            return self._run(check=False)

    def _run(self, *, check):
        self.output(KEYCHAIN_GUIDANCE)
        with self.client.native.noninteractive() if check else _lock(self.client.config):
            state = inspect_state(self.client)
        saved_token(self.account_file, DEFAULT_SHARE_SERVER)
        wrapper_ready = True
        try:
            self.verify_wrapper()
        except ClientError:
            wrapper_ready = False
        self.service_spec(self.loaded(self.service))
        if check:
            self.output("Readiness checked: saved Claude Share login exists; "
                        + ("native launcher is installed." if wrapper_ready else "native launcher will be installed with administrator approval.")
                        + " Server membership has not been checked. No configuration was changed.")
            return {"check": True, "enrolled": state is not None, "wrapper_ready": wrapper_ready}
        identity = self.membership(self.account_file)
        if state and state.get("share_account_id") and (state["share_account_id"] != identity or state.get("share_team_id") != TEAM_ID):
            raise ClientError("This Mac is linked to a different Claude Share identity. Sign in to its original Share account; setup will not rebind it.")
        self.stop_team()
        self.reopen_desktop = False
        try:
            if not wrapper_ready:
                self.install_wrapper()
            self.stop_personal()
            # Repeat inspection after stopping competing writers; never accept drift.
            with _lock(self.client.config):
                state = inspect_state(self.client)
            self.enroll(self.client, account_file=self.account_file,
                        device_name=socket.gethostname(), link=state is not None)
            self.client.sync(take_over_native_store=not bool(state and state.get("managed")))
            synced = self.client._load()
            if synced.get("state") != "active":
                raise ClientError("The pool has no confirmed usable assignment. Inspect team-pool-client status and repeat setup when quota is available.")
            # Exercise the same noninteractive guards as the LaunchAgent before
            # installing it. This does not approve or bypass a Keychain dialog.
            with self.client.native.noninteractive():
                self.client.sync()
            foreground_synced_at = self.client._load()["synced_at"]
            result = self.command(["/bin/launchctl", "enable", self.service])
            if result.returncode:
                raise ClientError("Could not enable background synchronization. Existing pool enrollment was preserved.")
            # Retain the old service file until the replacement runtime has
            # passed foreground and noninteractive validation.
            loaded = self.loaded(self.service)
            if loaded is not None:
                raise ClientError("A team service restarted during setup. Preserve its service file and stop it before retrying.")
            previous_service = self.service_spec(None)
            previous_bytes = _private_read(previous_service) if previous_service else None
            if previous_service:
                previous_service.unlink()
            try:
                self.installer(self.client, home=self.home, runner=self.runner, python=sys.executable)
            except BaseException:
                if previous_bytes is not None:
                    if not previous_service.exists() and not previous_service.is_symlink():
                        _private_write(previous_service, previous_bytes)
                        self.output("The previous service configuration was restored. Repeat setup to start background sync.")
                    else:
                        backup = self.client.config.parent / ("setup-service-backup-" + uuid.uuid4().hex + ".plist")
                        _private_write(backup, previous_bytes)
                        self.output("The previous service configuration was preserved at " + str(backup))
                raise
            self.background_ready(foreground_synced_at)
            result = self.command([sys.executable, str(self.source / "scripts/install_team_status.py"), "install"], visible=True, timeout=180)
            if result.returncode:
                raise ClientError("The pool is active, but the menu bar installation failed. Repeat setup to finish installing the menu bar.")
            self.output("Setup complete: Paradigm shared pool is active in native subscription mode; background sync was verified.")
            return self.client.status()
        except BaseException:
            try:
                stopped = self.loaded(self.service) is None
            except ClientError:
                stopped = False
            if stopped:
                self.output("Background synchronization is stopped. Resolve the error and run the same setup command again.")
            raise
        finally:
            if self.reopen_desktop:
                result = self.command(["/usr/bin/open", "-b", "com.anthropic.claudefordesktop"])
                if result.returncode:
                    self.output("Reopen Claude Desktop from Applications when ready.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--account-file", type=Path, default=DEFAULT_ACCOUNT_FILE)
    parser.add_argument("--check", action="store_true", help="Read local readiness without changing configuration or contacting servers")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin" or os.geteuid() == 0:
            raise ClientError("Run setup in Terminal as your ordinary macOS user, without sudo.")
        # Only this process is changed. The user's Codex gateway remains intact.
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        Setup(args.source_dir, account_file=args.account_file).run(check=args.check)
        return 0
    except ClientError as error:
        print("Setup stopped: " + str(error), file=sys.stderr)
        return 2
    except Exception:
        print("Setup stopped: local setup or Keychain access could not complete. Existing recovery state was retained. Check the prerequisites and try again; no credential values were logged.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
