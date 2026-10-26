"""Opt-in native subscription pilot client. No inference traffic passes here."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import getpass
import ipaddress
import json
import math
import os
from pathlib import Path
import plistlib
import random
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_CONFIG = Path.home() / ".config/claude-desktop-swap/team.json"
SERVICE = "study.paradigm.claude-desktop-swap.team"
DEVICE_SECRET = "device-token"
MAX_RESPONSE = 64 * 1024


class ClientError(Exception):
    """An error whose text is safe to display; never wrap raw remote errors."""


class RequestError(ClientError):
    def __init__(self, status=None):
        self.status = status
        super().__init__("Coordinator request failed" + (f" (HTTP {status})." if status else "."))


class PausedError(ClientError):
    pass


def server_url(value, allow_loopback_http=False):
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if not host or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if "?" in value or "#" in value or parsed.path not in ("", "/"):
            raise ValueError
        if any(c.isspace() for c in value) or "\\" in value:
            raise ValueError
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and allow_loopback_http and loopback
        ):
            raise ValueError
        if port is not None and not 0 < port < 65536:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ClientError("Use an HTTPS coordinator origin without credentials, path, query, or fragment.") from None
    return value.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    """Certificate-verified HTTPS, bounded JSON, and no bearer redirects."""

    def __init__(self, server, *, allow_loopback_http=False, timeout=75):
        self.server = server_url(server, allow_loopback_http)
        self.timeout = timeout
        handlers = [_NoRedirect()]
        if urllib.parse.urlsplit(self.server).scheme == "http":
            # Explicit local fixtures must not send their cleartext bearer to a proxy.
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def post(self, path, body, token=None):
        if path not in ("/v1/enroll", "/v1/enroll/share", "/v1/link/share", "/v1/assignment", "/v1/contributions/start",
                        "/v1/contributions/complete", "/v1/contributions/status",
                        "/v1/contributions/cancel"):
            raise ClientError("Unknown coordinator endpoint.")
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "claude-desktop-swap-pilot/1"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            self.server + path, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise RequestError()
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise RequestError()
                return result
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise RequestError(status) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise RequestError() from None


def _private_read(path):
    """Refuse symlinks, non-owned files, and group/world readable secret inputs."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ClientError("Input and configuration files must be owned by you and have mode 0600.")
            raw = stream.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ClientError("Input file is too large.")
            return raw
    except OSError:
        raise ClientError("Cannot securely read the input or configuration file.") from None


def _private_write(path, raw):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise ClientError("Refusing a symbolic-link destination.")
    if path.exists():
        _private_read(path)
    fd, temporary = tempfile.mkstemp(prefix=".team-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    stream = None
    try:
        fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        stream = os.fdopen(fd, "a")
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ClientError("Unsafe client lock file.")
        fcntl.flock(stream, fcntl.LOCK_EX)
    except OSError:
        if stream:
            stream.close()
        raise ClientError("Cannot lock the client state.") from None
    except BaseException:
        if stream:
            stream.close()
        raise
    try:
        yield
    finally:
        stream.close()


def _configuration(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ClientError("Desktop native launcher configuration is missing or invalid; check the desktop-swap installation.") from None


def verify_desktop_wrapper(*, home=None, system_dir=None):
    home = Path(home or Path.home())
    system_dir = Path(system_dir or "/Library/Application Support/ClaudeCode")
    profiles = home / "Library/Application Support/Claude-3p/configLibrary"
    selected = _configuration(profiles / "_meta.json").get("appliedId")
    if not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", selected):
        raise ClientError("Select a supported Claude Desktop profile before starting the native pilot.")
    profile = _configuration(profiles / (selected + ".json"))
    if profile.get("claudeCodeProcessWrapperEnabled") is not True:
        raise ClientError("Enable the desktop-swap launcher for the selected Desktop profile first.")
    launcher = system_dir / "native-subscription-launcher"
    wrapper = _configuration(system_dir / "managed-settings.json").get("processWrapper")
    try:
        if not isinstance(wrapper, str) or json.loads(wrapper) != [str(launcher)]:
            raise ValueError
        fd = os.open(launcher, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                raise ValueError
            text = stream.read(16385).decode("utf-8")
        if len(text) > 16384 or f'if [ "$(/usr/bin/id -u)" = "{os.getuid()}" ]; then' not in text:
            raise ValueError
        for name in ("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
                     "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                     "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            if not re.search(r"\bunset\s+[^\n]*\b" + name + r"\b", text):
                raise ValueError
        if 'exec "$@"' not in text:
            raise ValueError
    except (OSError, ValueError):
        raise ClientError("The desktop-swap launcher is missing, changed, or installed for another user.") from None


def native_preflight():
    """A stopped cswap service is fine; a loaded selector would fight this client."""
    if sys.platform != "darwin":
        raise ClientError("Native credential synchronization requires macOS.")
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/com.cswap.menubar"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode == 0:
        raise ClientError("Stop the cswap menu-bar service before managing its native credential store.")
    verify_desktop_wrapper()


def _safe_string(value, *, maximum=4096):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


class Client:
    """Stateful client; inject native/transport/preflight/clock for offline tests."""

    def __init__(self, config=DEFAULT_CONFIG, *, native=None, transport=None,
                 clock=time.time, preflight=native_preflight):
        self.config = Path(config).expanduser().absolute()
        if native is None:
            from .native import NativeStore
            native = NativeStore()
        self.native = native
        self.transport = transport
        self.clock = clock
        self.preflight = preflight

    def _load(self, optional=False):
        if optional and not self.config.exists() and not self.config.is_symlink():
            return None
        try:
            result = json.loads(_private_read(self.config))
            if not isinstance(result, dict) or result.get("version") != 1:
                raise ValueError
            server_url(result["server"], result.get("allow_loopback_http", False))
            if not _safe_string(result["device_id"], maximum=256):
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError):
            raise ClientError("Client state is invalid; preserve it for recovery before re-enrolling.") from None

    def _save(self, state):
        _private_write(self.config, (json.dumps(state, sort_keys=True, indent=2) + "\n").encode())

    def _transport(self, state):
        return self.transport or Transport(state["server"], allow_loopback_http=state.get("allow_loopback_http", False))

    def _keychain_access_required(self, state=None):
        message = ("Keychain access requires foreground authorization. Run resume and then sync --once "
                   "in Terminal, approve the expected Keychain request, then restart the service.")
        if state is not None:
            state.update(state="paused", reason="keychain_access_required")
            self._save(state)
            raise PausedError(message) from None
        raise ClientError(message) from None

    def _preflight(self, state=None):
        from .native import NativeKeychainAccessError, NativePolicyError, NativeStoreError
        try:
            self.preflight()
            self.native.preflight()
        except ClientError:
            if state is not None:
                state.update(state="paused", reason="native_setup_or_policy")
                self._save(state)
                raise PausedError("Native setup or policy changed. Correct it, then explicitly resume the client.") from None
            raise
        except NativePolicyError:
            if state is not None:
                state.update(state="paused", reason="native_setup_or_policy")
                self._save(state)
                raise PausedError("Native setup or policy changed. Correct it, then explicitly resume the client.") from None
            raise ClientError("Native setup or policy preflight failed; verify the launcher, local selectors, and authentication settings.") from None
        except NativeKeychainAccessError:
            self._keychain_access_required(state)
        except NativeStoreError:
            raise ClientError("Native credential preflight failed; verify Keychain access.") from None

    def enroll(self, server, device_name, invitation, *, allow_loopback_http=False):
        server = server_url(server, allow_loopback_http)
        if not _safe_string(device_name, maximum=128) or not _safe_string(invitation):
            raise ClientError("A device name and valid invitation are required.")
        with _lock(self.config):
            if self._load(optional=True) is not None:
                raise ClientError("Enrollment or recovery state already exists. Complete leave --restore before enrolling again.")
            self._preflight()
            state = {"version": 1, "server": server, "allow_loopback_http": allow_loopback_http}
            response = self._transport(state).post("/v1/enroll", {"invitation": invitation, "device_name": device_name})
            if not _safe_string(response.get("device_id"), maximum=256) or not _safe_string(response.get("device_token")):
                raise ClientError("Coordinator returned an invalid enrollment response.")
            state.update(device_id=response["device_id"], state="enrolled", managed=False)
            self.native.set_secret(DEVICE_SECRET, response["device_token"])
            try:
                self._save(state)
            except Exception:
                self.native.delete_secret(DEVICE_SECRET)
                raise
            return self._status(state)

    def sync(self, *, take_over_native_store=False):
        from .native import NativeDriftError, NativeKeychainAccessError, NativePayloadError, NativePolicyError, NativeStoreError
        with _lock(self.config):
            state = self._load()
            if state["state"] in ("revoked", "left"):
                raise PausedError("Enrollment was revoked or left. Inspect status and follow the recovery runbook.")
            if state["state"] == "paused":
                raise PausedError("Client is paused. Inspect status and explicitly resume after correcting the reported condition.")
            self._preflight(state)
            try:
                snapshot = self.native.snapshot()
            except NativeKeychainAccessError:
                self._keychain_access_required(state)
            except NativeStoreError:
                raise ClientError("Cannot inspect native credential state; no assignment was requested.") from None
            self._recover_pending(state, snapshot)
            if state["state"] == "paused":
                raise PausedError("Client is paused. Inspect status and follow the recovery runbook.")
            initialize = not state.get("managed", False)
            if initialize and snapshot["has_credentials"] and not take_over_native_store:
                raise ClientError("Existing native credentials require an explicit --take-over-native-store first sync.")
            if not initialize and snapshot["fingerprint"] != state["fingerprint"]:
                state.update(state="paused", reason="native_credentials_changed")
                self._save(state)
                raise PausedError("Native credentials changed. Synchronization paused without overwriting them.")
            try:
                token = self.native.get_secret(DEVICE_SECRET)
            except NativeKeychainAccessError:
                self._keychain_access_required(state)
            if not _safe_string(token):
                raise ClientError("Device credential is missing. Leave and enroll with a new invitation.")
            try:
                assignment = self._transport(state).post("/v1/assignment", {}, token=token)
            except RequestError as error:
                if error.status == 401:
                    state.update(state="revoked", reason="device_revoked")
                else:
                    state.update(state="degraded", reason="coordinator_unavailable")
                self._save(state)
                raise
            self._validate_assignment(assignment)
            expected = snapshot["fingerprint"] if initialize else state["fingerprint"]
            try:
                plan = self.native.plan(assignment, expected, initialize=initialize)
                if not isinstance(plan, dict) or any(
                    not isinstance(plan.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", plan[key])
                    for key in ("before", "keychain_only", "after")
                ) or plan["before"] != expected:
                    raise ClientError("Native adapter returned an invalid write plan; nothing was written.")
                pending = {key: plan[key] for key in ("before", "keychain_only", "after")}
                pending.update(self._assignment_metadata(assignment))
                state["pending_write"] = pending
                if not initialize and plan["before"] == plan["keychain_only"] == plan["after"]:
                    # plan re-reads under native locks and validates policy,
                    # expiry, ownership and backup. The credential is unchanged:
                    # commit only fresh coordinator metadata, without a Keychain
                    # write or a .claude.json rewrite on every polling interval.
                    self._finish_pending(state)
                    return self._status(state)
                self._save(state)
                fingerprint = self.native.apply(assignment, expected, initialize=initialize)
            except NativeDriftError:
                state.update(state="paused", reason="native_credentials_changed")
                self._save(state)
                raise PausedError("Native credentials changed. Synchronization paused without overwriting them.") from None
            except NativePayloadError:
                state.update(state="paused", reason="native_store_capacity")
                self._save(state)
                raise PausedError("The complete native credential payload exceeds the supported security(1) format or size. Existing credentials and MCP/plugin data were preserved.") from None
            except NativePolicyError:
                state.update(state="paused", reason="native_setup_or_policy")
                self._save(state)
                raise PausedError("Native policy blocks this assignment; correct the policy or pool, then explicitly resume.") from None
            except NativeKeychainAccessError:
                self._keychain_access_required(state)
            except NativeStoreError:
                raise ClientError("Could not update the native credential store. Enrollment is preserved.") from None
            if fingerprint != pending["after"]:
                state.update(state="paused", reason="native_credentials_changed")
                self._save(state)
                raise PausedError("Native write result differed from the durable plan; recovery state was retained.")
            self._finish_pending(state)
            return self._status(state)

    def _assignment_metadata(self, assignment):
        metadata = {
            "account_id": assignment["account_id"], "generation": assignment["generation"],
            "account_email": assignment["account"]["emailAddress"],
            "expires_at": assignment["expires_at"], "synced_at": self.clock(),
            "renew_after": min(300, max(5, assignment["renew_after"])),
            "state": "degraded" if assignment.get("state") in ("quota_stale", "quota_exhausted") else "active",
        }
        if assignment.get("state") in ("quota_stale", "quota_exhausted"):
            metadata["reason"] = assignment["state"]
        return metadata

    def _finish_pending(self, state):
        pending = state["pending_write"]
        state.update(managed=True, fingerprint=pending["after"])
        state.pop("reason", None)
        for key in ("account_id", "account_email", "generation", "expires_at", "synced_at", "renew_after", "state", "reason"):
            if key in pending:
                state[key] = pending[key]
        state.pop("pending_write")
        self._save(state)

    def _recover_pending(self, state, snapshot):
        pending = state.get("pending_write")
        if pending is None:
            return
        current = snapshot["fingerprint"]
        # Check completed first: token-only renewal can have after == keychain_only.
        if current == pending["after"] and snapshot["has_backup"]:
            self._finish_pending(state)
        elif current == pending["before"]:
            state.pop("pending_write")
            self._save(state)
        else:
            reason = "partial_native_write" if current == pending["keychain_only"] else "native_credentials_changed"
            state.update(state="paused", reason=reason)
            self._save(state)
            if reason == "partial_native_write":
                raise PausedError("An interrupted native write needs explicit leave --restore; recovery state was retained.")
            raise PausedError("Native credentials changed outside the recorded write; no recovery overwrite was attempted.")

    def _validate_assignment(self, value):
        if (
            not _safe_string(value.get("account_id"), maximum=256)
            or not isinstance(value.get("generation"), int) or isinstance(value["generation"], bool)
            or value["generation"] < 0
            or not _safe_string(value.get("access_token"), maximum=16384)
            or not _number(value.get("expires_at")) or value["expires_at"] <= self.clock()
            or not _number(value.get("renew_after")) or value["renew_after"] <= 0
            or not isinstance(value.get("scopes"), list)
            or not all(_safe_string(scope, maximum=256) for scope in value["scopes"])
            or not isinstance(value.get("account"), dict)
            or not _safe_string(value["account"].get("emailAddress"), maximum=320)
            or value.get("state", "fresh") not in ("fresh", "quota_stale", "quota_exhausted")
        ):
            raise ClientError("Coordinator returned an invalid or expired assignment.")

    def _status(self, state):
        result = {key: state[key] for key in ("state", "device_id", "account_id", "account_email", "expires_at", "reason") if key in state}
        if state.get("share_team_id") and state.get("share_account_id"):
            result["membership_linked"] = True
            result["share_team"] = state.get("share_team_name", state["share_team_id"])
        if state.get("expires_at", float("inf")) <= self.clock():
            result["credential_expired"] = True
            if result["state"] not in ("paused", "revoked", "left"):
                result["state"] = "expired"
        else:
            result["credential_expired"] = False
        if state.get("synced_at") is not None:
            result["next_sync_in"] = max(0, int(state["synced_at"] + state["renew_after"] - self.clock()))
        if "pending_write" in state:
            result["write_recovery_pending"] = True
        return result

    def status(self):
        with _lock(self.config):
            state = self._load(optional=True)
            return self._status(state) if state else {"state": "not_enrolled"}

    def resume(self):
        """Acknowledge repaired setup; never overwrite or accept native-login drift."""
        from .native import NativeKeychainAccessError
        with _lock(self.config):
            state = self._load()
            if state["state"] in ("revoked", "left"):
                raise ClientError("Revoked or departed enrollment cannot be resumed.")
            self._preflight(state)
            try:
                snapshot = self.native.snapshot()
                token = self.native.get_secret(DEVICE_SECRET)
            except NativeKeychainAccessError:
                self._keychain_access_required(state)
            if not _safe_string(token):
                raise ClientError("Device credential is missing. Leave and enroll with a new invitation.")
            self._recover_pending(state, snapshot)
            if state.get("managed") and snapshot["fingerprint"] != state["fingerprint"]:
                raise ClientError("Native credentials still differ; resume will not overwrite a manual login or logout.")
            state["state"] = "enrolled"
            state.pop("reason", None)
            self._save(state)
            return self._status(state)

    def leave(self, *, restore=False):
        from .native import NativeStoreError
        with _lock(self.config):
            state = self._load(optional=True)
            restored = False
            if restore and state and (state.get("managed") or state.get("pending_write")):
                try:
                    snapshot = self.native.snapshot()
                    allowed = {state["fingerprint"]} if state.get("fingerprint") else set()
                    pending = state.get("pending_write")
                    if pending:
                        allowed.update(pending[key] for key in ("before", "keychain_only", "after"))
                    if snapshot["fingerprint"] not in allowed:
                        raise NativeStoreError("Unrelated native drift.")
                    if snapshot["has_backup"]:
                        self.native.restore(snapshot["fingerprint"])
                        restored = True
                    elif not pending or snapshot["fingerprint"] != pending["before"]:
                        raise NativeStoreError("Missing native recovery backup.")
                except NativeStoreError:
                    raise ClientError("Native restoration refused; preserve state and inspect credential drift.") from None
            if state and not restore:
                state.update(state="left", reason="recovery_retained")
                self._save(state)
            self.native.delete_secret(DEVICE_SECRET)
            if state and restore:
                self.config.unlink()
            return {"state": "left", "native_restored": restored, "recovery_retained": bool(state and not restore)}


def service_path(home=None):
    return Path(home or Path.home()) / "Library/LaunchAgents" / (SERVICE + ".plist")


def install_service(client, *, home=None, runner=subprocess.run, python=None):
    if sys.platform != "darwin":
        raise ClientError("LaunchAgent installation requires macOS.")
    state = client._load()
    if not state.get("managed") or state["state"] in ("paused", "revoked", "left") or state.get("pending_write"):
        raise ClientError("Complete a successful first sync before installing the service.")
    client.preflight()
    destination = service_path(home)
    if destination.exists() or destination.is_symlink():
        raise ClientError("The team service already exists; remove it before reinstalling.")
    log = client.config.parent / "agent.log"
    if not log.exists():
        _private_write(log, b"")
    else:
        _private_read(log)
    specification = {
        "Label": SERVICE,
        "ProgramArguments": [os.path.abspath(python or sys.executable), "-m", "team_pool.client", "--config", str(client.config), "run"],
        "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
        "RunAtLoad": True,
        "StandardOutPath": str(log), "StandardErrorPath": str(log), "Umask": 0o077,
    }
    _private_write(destination, plistlib.dumps(specification))
    result = runner(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode:
        destination.unlink()
        raise ClientError("LaunchAgent could not start; the service file was removed.")


def remove_service(*, home=None, runner=subprocess.run):
    if sys.platform != "darwin":
        raise ClientError("LaunchAgent removal requires macOS.")
    destination = service_path(home)
    if not destination.exists() and not destination.is_symlink():
        return
    raw = _private_read(destination)
    try:
        if plistlib.loads(raw).get("Label") != SERVICE:
            raise ValueError
    except (ValueError, plistlib.InvalidFileException):
        raise ClientError("Refusing to remove an unrecognized service file.") from None
    runner(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    destination.unlink()


def run(client, *, sleep=time.sleep, jitter=random.uniform):
    from .native import NativeStoreError
    try:
        with client.native.noninteractive():
            return _run(client, sleep=sleep, jitter=jitter)
    except NativeStoreError:
        # Refuse to run if the background guards fail; do not retry without them
        # or expose backend exception text.
        print("Background synchronization stopped: noninteractive Keychain access could not complete.", flush=True)
        return 2


def _run(client, *, sleep, jitter):
    failures = 0
    last_state = None
    while True:
        try:
            status = client.sync()
            failures = 0
            delay = max(5, status.get("next_sync_in", 30))
        except PausedError:
            status = client.status()
            if status.get("reason") == "keychain_access_required":
                print("Client paused: Keychain access requires foreground authorization. Run resume and sync --once in Terminal before restarting the service.", flush=True)
            else:
                print("Client paused. Inspect status and correct the reported condition.", flush=True)
            return 2
        except ClientError:
            failures += 1
            status = client.status()
            if status["state"] == "revoked":
                print("Client stopped: enrollment revoked.", flush=True)
                return 2
            delay = min(300, 5 * 2 ** min(failures - 1, 6))
        current = (status["state"], status.get("reason"))
        if current != last_state:
            print("Client state: " + status["state"] + ".", flush=True)
            last_state = current
        sleep(max(5, min(300, delay * jitter(0.8, 1.2))))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--server", required=True)
    enroll.add_argument("--device-name", required=True)
    enroll.add_argument("--invitation-file", type=Path)
    enroll.add_argument("--allow-loopback-http", action="store_true", help="Local test coordinators only")
    from .share_enrollment import DEFAULT_ACCOUNT_FILE, DEFAULT_SHARE_SERVER
    for name in ("enroll-share", "link-share"):
        share = commands.add_parser(name, help="Use this Mac's Claude Share team sign-in instead of an invitation")
        share.add_argument("--share-server", default=DEFAULT_SHARE_SERVER)
        share.add_argument("--device-name")
        share.add_argument("--account-file", type=Path, default=DEFAULT_ACCOUNT_FILE)
    sync = commands.add_parser("sync")
    sync.add_argument("--once", action="store_true", required=True)
    sync.add_argument("--take-over-native-store", action="store_true")
    for command in ("run", "status", "resume", "service-install", "service-remove"):
        commands.add_parser(command)
    leave = commands.add_parser("leave")
    leave.add_argument("--restore", action="store_true")
    contribute = commands.add_parser("contribute-account", help="Share a fresh subscription login with the enrolled pool")
    contribution_options = contribute.add_mutually_exclusive_group()
    contribution_options.add_argument("--no-browser", action="store_true", help="Print the authorization URL for manual opening")
    contribution_options.add_argument("--status", action="store_true", help="Recover the last contribution receipt without exchanging a code")
    contribution_options.add_argument("--cancel", action="store_true", help="Cancel a pending contribution login")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise ClientError("The native pool client requires macOS.")
        client = Client(args.config)
        if args.command == "enroll":
            invitation = _private_read(args.invitation_file).decode().strip() if args.invitation_file else getpass.getpass("Single-use invitation: ")
            result = client.enroll(args.server, args.device_name, invitation, allow_loopback_http=args.allow_loopback_http)
        elif args.command in ("enroll-share", "link-share"):
            from .share_enrollment import enroll_from_share
            result = enroll_from_share(client, share_server=args.share_server,
                                       device_name=args.device_name, account_file=args.account_file,
                                       link=args.command == "link-share")
        elif args.command == "sync":
            result = client.sync(take_over_native_store=args.take_over_native_store)
        elif args.command == "status":
            result = client.status()
        elif args.command == "resume":
            result = client.resume()
        elif args.command == "contribute-account":
            from .contribute import run_contribution
            result = run_contribution(client, no_browser=args.no_browser,
                                      status=args.status, cancel=args.cancel)
        elif args.command == "run":
            return run(client)
        elif args.command == "service-install":
            install_service(client)
            result = {"state": "service_installed"}
        elif args.command == "service-remove":
            remove_service()
            result = {"state": "service_removed"}
        else:
            remove_service()
            result = client.leave(restore=args.restore)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ClientError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Keychain/backend failures may embed credentials. Do not display tracebacks.
        print("Client operation failed. Check installation and Keychain access; existing state was preserved where possible.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Collaborating CLI modules import this package's ClientError. Use the same
    # module instance under -m so their safe diagnostics reach the typed handler.
    from .client import main as cli_main
    raise SystemExit(cli_main())
