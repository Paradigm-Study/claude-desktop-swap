"""Conservative adapter for Claude Code's default macOS native credential store.

This module never refreshes an OAuth token. The coordinator owns that grant;
clients receive only an access token. Native store formats and advisory locks
are version-sensitive. See the pilot runbook before using a real Keychain.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import plistlib
import pwd
import re
import stat
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path


NATIVE_SERVICE = "Claude Code-credentials"
PILOT_SERVICE = "Claude Desktop Swap Pilot"
SHARED_KEYS = frozenset({
    "mcpOAuth", "mcpOAuthClientConfig", "mcpXaaIdp", "mcpXaaIdpConfig", "pluginSecrets",
})
AUTH_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SECURE_STORE_DIR", "CLAUDE_CODE_PROFILE",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
})


class NativeStoreError(RuntimeError):
    """A local native store operation could not safely complete."""


class NativeKeychainAccessError(NativeStoreError):
    """Foreground approval or an unlocked Keychain is required."""

    def __init__(self, status, *, reason="access_required"):
        self.status = status
        self.reason = "locked" if type(reason) is str and reason == "locked" else "access_required"
        message = ("The native Keychain is locked." if self.reason == "locked"
                   else "Keychain access requires foreground authorization.")
        super().__init__(message)


class NativeDriftError(NativeStoreError):
    """Another writer changed the native account; do not overwrite it."""


class NativePolicyError(NativeStoreError):
    """An auth override or policy requires a different configuration."""


class NativePayloadError(NativePolicyError):
    """The complete native payload exceeds the safe writer's format or capacity."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


_FALLBACK_LIMIT = 4 * 1024 * 1024


def _strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError

    try:
        value = json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise NativePolicyError("Credential fallback recovery contains unsupported JSON; no data was discarded.") from None


def _private_blob(path: Path, limit=_FALLBACK_LIMIT, *, durable=False):
    """Read a bounded owned private regular file without following links."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > limit):
                raise ValueError
            data = stream.read(limit + 1)
            if durable:
                os.fsync(stream.fileno())
            after = os.fstat(stream.fileno())
        current = path.lstat()
        if (len(data) > limit or _file_identity(before) != _file_identity(after)
                or _file_identity(before) != _file_identity(current)):
            raise ValueError
        return data, before
    except (OSError, ValueError):
        raise NativePolicyError("Credential fallback recovery requires an unchanged, owned private regular file.") from None


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _regular_json(path: Path) -> dict:
    """Reject symlinks and malformed files instead of overwriting them."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise NativeStoreError(f"Cannot safely read {path.name}.") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise NativeStoreError(f"{path.name} is not a regular file.")
            data = stream.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError
        parsed = plistlib.loads(data) if path.suffix == ".plist" else json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError
        return parsed
    except (ValueError, plistlib.InvalidFileException):
        raise NativeStoreError(f"{path.name} is malformed; nothing was replaced.") from None


def _safe_directory(path: Path):
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError
        if info.st_mode & 0o022:
            raise OSError
    except OSError:
        raise NativeStoreError(f"{path.name} must be an owned, non-writable-by-others directory.") from None


@contextmanager
def _directory_lock(path: Path, timeout: float):
    """Cooperate with proper-lockfile. Never steal an existing lock in the pilot."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.mkdir(mode=0o700)
            identity = path.stat().st_ino
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise NativeStoreError(f"Native lock {path.name} is busy; retry after its owner exits.") from None
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    stop = threading.Event()
    lost = threading.Event()

    def check():
        try:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_ino != identity or lost.is_set():
                raise OSError
        except OSError:
            raise NativeStoreError("A native credential lock was lost; stop the pilot agent.") from None

    def heartbeat():
        while not stop.wait(2):
            try:
                check()
                os.utime(path, follow_symlinks=False)
            except (OSError, NativeStoreError):
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield check
    finally:
        stop.set()
        thread.join(timeout=3)
        try:
            check()
            path.rmdir()
        except (OSError, NativeStoreError):
            pass


class NativeStore:
    def __init__(self, home: Path | None = None, backend=None, *, policy_paths=None,
                 environ=None, project_dirs=(), clock=time.time, lock_timeout=9.0):
        self.home = Path(home) if home is not None else Path.home()
        self.config = self.home / ".claude.json"
        self.claude = self.home / ".claude"
        self.environ = dict(os.environ if environ is None else environ)
        self.username = self.environ.get("USER") or pwd.getpwuid(os.geteuid()).pw_name
        self.clock = clock
        self.lock_timeout = lock_timeout
        self.project_dirs = [Path(p) for p in project_dirs]
        self.policy_paths = list(policy_paths) if policy_paths is not None else [
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
            Path("/Library/Managed Preferences/com.anthropic.claudecode.plist"),
            Path("/Library/Managed Preferences") / self.username / "com.anthropic.claudecode.plist",
        ]
        if backend is None:
            if sys.platform != "darwin":
                raise NativeStoreError("The native adapter requires macOS and an unlocked login Keychain.")
            if os.geteuid() == 0:
                raise NativeStoreError("Run the native pilot agent as the logged-in user, without sudo.")
            try:
                from .macos_keychain import MacOSKeychain
                backend = MacOSKeychain()
            except ImportError:
                raise NativeStoreError("Install the pilot's macOS keyring dependency first.") from None
        self.backend = backend

    def _get(self, service, account):
        try:
            return self.backend.get_password(service, account)
        except Exception as error:
            from .macos_keychain import access_required_reason, access_required_status
            status = access_required_status(error)
            if status is not None:
                reason = access_required_reason(error) if service == NATIVE_SERVICE else "access_required"
                raise NativeKeychainAccessError(status, reason=reason) from None
            raise NativeStoreError("Keychain read failed or was denied; no plaintext fallback is used.") from None

    def _set(self, service, account, value):
        try:
            if value is None:
                if self.backend.get_password(service, account) is not None:
                    self.backend.delete_password(service, account)
            else:
                self.backend.set_password(service, account, value)
                if self.backend.get_password(service, account) != value:
                    raise ValueError
        except Exception as error:
            from .macos_keychain import access_required_reason, access_required_status
            status = access_required_status(error)
            if status is not None:
                reason = access_required_reason(error) if service == NATIVE_SERVICE else "access_required"
                raise NativeKeychainAccessError(status, reason=reason) from None
            raise NativeStoreError("Keychain write failed or could not be verified.") from None

    @contextmanager
    def noninteractive(self):
        """Suppress in-process UI and reject known interactive native CLI access.

        The native CLI has a metadata preflight and bounded timeout; a concurrent
        lock or ACL change can still race that check and cause one prompt.
        """
        from .macos_keychain import KeychainInteractionError
        try:
            with self.backend.noninteractive():
                yield
        except KeychainInteractionError:
            raise NativeStoreError("Cannot establish noninteractive Keychain access; background synchronization stopped.") from None

    def _secret_account(self, name):
        if not isinstance(name, str) or not name or len(name) > 80 or name == "native-backup":
            raise NativeStoreError("Invalid pilot secret name.")
        return self.username + ":" + name

    def get_secret(self, name: str) -> str | None:
        return self._get(PILOT_SERVICE, self._secret_account(name))

    def set_secret(self, name: str, value: str):
        self._set(PILOT_SERVICE, self._secret_account(name), value)

    def delete_secret(self, name: str):
        self._set(PILOT_SERVICE, self._secret_account(name), None)

    def _backup(self):
        raw = self._get(PILOT_SERVICE, self.username + ":native-backup")
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if (value.get("version") != 1 or not isinstance(value.get("credentials"), dict)
                    or not isinstance(value.get("oauth_present"), bool)
                    or not isinstance(value.get("fingerprint"), str)
                    or len(value["fingerprint"]) != 64
                    or "oauthAccount" not in value):
                raise ValueError
            return value
        except (ValueError, AttributeError):
            raise NativeStoreError("The native backup is invalid; it was not overwritten.") from None

    def _read(self):
        raw = self._get(NATIVE_SERVICE, self.username)
        try:
            credentials = json.loads(raw) if raw is not None else {}
            if not isinstance(credentials, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise NativeStoreError("Native credentials are not a supported JSON object.") from None
        config = _regular_json(self.config)
        return credentials, config

    @staticmethod
    def _fingerprint(credentials, config):
        # MCP/plugin credentials, display metadata, and project settings can be
        # refreshed by Claude without changing who owns the native login.
        owned = {k: v for k, v in credentials.items() if k not in SHARED_KEYS}
        account = config.get("oauthAccount")
        identity = ({key: account.get(key) for key in ("accountUuid", "organizationUuid")}
                    if isinstance(account, dict) else account)
        data = {"credentials": owned, "oauth_present": "oauthAccount" in config,
                "oauthIdentity": identity}
        return hashlib.sha256(_json(data).encode()).hexdigest()

    def snapshot(self) -> dict:
        credentials, config = self._read()
        return {"fingerprint": self._fingerprint(credentials, config),
                "has_credentials": bool(credentials.get("claudeAiOauth")),
                "has_backup": self._backup() is not None}

    def preflight(self, assignment: dict | None = None, *, allow_redundant_fallback=False):
        if any(self.environ.get(key) for key in AUTH_ENV):
            raise NativePolicyError("Auth/provider/config-directory overrides are unsupported by the native pilot.")
        if self.claude.is_symlink() or self.config.is_symlink():
            raise NativePolicyError("Symlinked native configuration is unsupported by the pilot.")
        recovery = self._fallback_journal()
        if os.path.lexists(self.claude / ".credentials.json") or recovery is not None:
            if not allow_redundant_fallback:
                raise NativePolicyError("A credential-file fallback or unfinished recovery exists; resolve it before enrolling.")
            self._inspect_redundant_fallback(recovery)
        if self._get("Claude Code", self.username) is not None:
            raise NativePolicyError("A native API-key Keychain item exists; use an ordinary subscription configuration.")
        paths = [Path(p) for p in self.policy_paths] + [self.claude / "settings.json"]
        for base in self.project_dirs:
            paths.extend([base / ".claude/settings.json", base / ".claude/settings.local.json"])
        # New managed fragment sources are deliberately not merged by this pilot.
        for path in paths:
            if path.name == "managed-settings.json" and (path.parent / "managed-settings.d").exists():
                raise NativePolicyError("Managed settings fragments require administrator review before this pilot.")
            settings = _regular_json(path)
            if settings.get("apiKeyHelper") or settings.get("awsAuthRefresh") or settings.get("awsCredentialExport"):
                raise NativePolicyError("An external auth helper is configured.")
            env = settings.get("env", {})
            if not isinstance(env, dict) or any(env.get(k) for k in AUTH_ENV):
                raise NativePolicyError("Settings contain unsupported auth/provider overrides.")
            method = settings.get("forceLoginMethod")
            if method is not None and method != "claudeai":
                raise NativePolicyError("Login policy does not permit this native subscription pilot.")
            if "allowedProviders" in settings:
                raise NativePolicyError("An allowedProviders policy requires administrator review.")
            for key in settings:
                if key.startswith("forceLogin") and key not in {"forceLoginMethod", "forceLoginOrgUUID"}:
                    raise NativePolicyError("An unrecognized forced-login policy is configured.")
            org = settings.get("forceLoginOrgUUID")
            if org is not None and assignment is not None and org != assignment.get("account", {}).get("organizationUuid"):
                raise NativePolicyError("The assigned account conflicts with the required organization.")
        if _regular_json(self.config).get("primaryApiKey"):
            raise NativePolicyError("A primary API key is configured.")

    def _fallback_directory(self):
        return self.claude / ".desktop-swap-fallback"

    def _check_fallback_directory(self, *, create=False):
        path = self._fallback_directory()
        try:
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError
            return info
        except FileNotFoundError:
            if not create:
                return None
            raise NativePolicyError("Credential fallback archive directory is unavailable.") from None
        except (OSError, ValueError):
            raise NativePolicyError("Credential fallback archive requires an owned private directory.") from None

    def _fallback_journal(self):
        if self._check_fallback_directory() is None:
            return None
        path = self._fallback_directory() / "pending.json"
        if not os.path.lexists(path):
            return None
        raw, _ = _private_blob(path, 4096)
        value = _strict_json(raw)
        if (set(value) != {"version", "archive", "sha256"} or type(value["version"]) is not int
                or value["version"] != 1 or not isinstance(value["archive"], str)
                or not re.fullmatch(r"[a-f0-9]{32}\.json", value["archive"])
                or not isinstance(value["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])):
            raise NativePolicyError("Credential fallback recovery journal is invalid; recovery remains paused.")
        return value

    def _fallback_native(self):
        raw = self._get(NATIVE_SERVICE, self.username)
        credentials = _strict_json(raw) if raw is not None else {}
        return credentials, _regular_json(self.config)

    @staticmethod
    def _redundant_connectors(raw, credentials):
        fallback = _strict_json(raw)
        if set(fallback) != {"mcpOAuth"} or not isinstance(fallback["mcpOAuth"], dict):
            raise NativePolicyError("Credential fallback contains account or unsupported data; it was retained.")
        native = credentials.get("mcpOAuth", {})
        if not isinstance(native, dict):
            raise NativePolicyError("Native connector data is unsupported; credential fallback was retained.")
        for name, connector in fallback["mcpOAuth"].items():
            stored = native.get(name)
            if (not name or not isinstance(connector, dict) or not isinstance(stored, dict)
                    or _json({k: v for k, v in connector.items() if k != "discoveryState"})
                    != _json({k: v for k, v in stored.items() if k != "discoveryState"})):
                raise NativePolicyError("Credential fallback has new or changed connector sign-ins; it was retained.")
        return len(fallback["mcpOAuth"])

    def _inspect_redundant_fallback(self, journal):
        source = self.claude / ".credentials.json"
        archive = self._fallback_directory() / journal["archive"] if journal else None
        source_exists = os.path.lexists(source)
        archive_exists = archive is not None and os.path.lexists(archive)
        if journal and source_exists == archive_exists:
            raise NativePolicyError("Credential fallback recovery is ambiguous; all data was retained.")
        path = archive if archive_exists else source
        raw, identity = _private_blob(path)
        if journal and hashlib.sha256(raw).hexdigest() != journal["sha256"]:
            raise NativePolicyError("Credential fallback changed during recovery; all data was retained.")
        credentials, _ = self._fallback_native()
        count = self._redundant_connectors(raw, credentials)
        return path, raw, identity, count

    def _write_fallback_journal(self, journal):
        directory = self._fallback_directory()
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(_json(journal) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "pending.json")
            _sync_directory(directory)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

    def retire_redundant_fallback(self, expected_fingerprint: str) -> dict:
        """Archive only duplicate MCP fallback data for an existing managed login.

        Native credentials, configuration, and the enrollment backup are never
        written. A durable metadata-only journal precedes the rename. Ordinary
        preflight rejects that journal until the moved bytes and current native
        connector data have both been verified, including after interruption.
        Advisory native locks bound this operation just like account activation;
        conflicting writers leave recovery paused with all data retained.
        """
        try:
            with self._locked() as check:
                self.preflight(allow_redundant_fallback=True)
                credentials, config = self._fallback_native()
                if not expected_fingerprint or self._fingerprint(credentials, config) != expected_fingerprint:
                    raise NativeDriftError("Native login changed; credential fallback was retained.")
                backup = self._backup()
                if backup is None:
                    raise NativeStoreError("No enrollment backup exists; credential fallback was retained.")
                journal = self._fallback_journal()
                source = self.claude / ".credentials.json"
                if journal is None and not os.path.lexists(source):
                    return {"status": "absent", "connectors": 0}
                path, raw, identity, count = self._inspect_redundant_fallback(journal)
                directory_info = self._check_fallback_directory(create=True)
                directory = self._fallback_directory()
                if journal is None:
                    journal = {"version": 1, "archive": uuid.uuid4().hex + ".json",
                               "sha256": hashlib.sha256(raw).hexdigest()}
                    self._write_fallback_journal(journal)
                archive = directory / journal["archive"]

                def verify_owner():
                    check()
                    now, now_config = self._fallback_native()
                    if (self._fingerprint(now, now_config) != expected_fingerprint
                            or _json(now) != _json(credentials) or self._backup() != backup):
                        raise NativeDriftError("Native login or connectors changed during fallback recovery; all data was retained.")
                    current_directory = self._check_fallback_directory()
                    if (current_directory is None or current_directory.st_dev != directory_info.st_dev
                            or current_directory.st_ino != directory_info.st_ino
                            or self._fallback_journal() != journal):
                        raise NativePolicyError("Credential fallback recovery changed; all data was retained.")

                verify_owner()
                unchanged, current_identity = _private_blob(path)
                if unchanged != raw or _file_identity(current_identity) != _file_identity(identity):
                    raise NativePolicyError("Credential fallback changed during recovery; all data was retained.")
                if path == source:
                    if os.path.lexists(archive):
                        raise NativePolicyError("Credential fallback archive already exists; all data was retained.")
                    os.rename(source, archive)
                    _sync_directory(directory)
                    _sync_directory(self.claude)
                verify_owner()
                archived, _ = _private_blob(archive, durable=True)
                if archived != raw or hashlib.sha256(archived).hexdigest() != journal["sha256"]:
                    raise NativePolicyError("Credential fallback archive verification failed; recovery remains paused.")
                self._redundant_connectors(archived, credentials)
                if os.path.lexists(source):
                    raise NativePolicyError("A new credential fallback appeared; all data was retained.")
                (directory / "pending.json").unlink()
                _sync_directory(directory)
                return {"status": "archived", "connectors": count}
        except OSError:
            raise NativeStoreError("Credential fallback recovery could not complete; existing data was retained.") from None

    @contextmanager
    def _locked(self):
        _safe_directory(self.home)
        _safe_directory(self.claude)
        with ExitStack() as stack:
            checks = [stack.enter_context(_directory_lock(p, self.lock_timeout)) for p in [
                self.claude / ".oauth_refresh.lock", self.home / ".claude.lock",
                self.home / ".claude.json.lock",
            ]]
            def check():
                for verify in checks:
                    verify()
            yield check

    def _write_config(self, config):
        # Same-directory replace preserves all non-account settings read under lock.
        _regular_json(self.config)
        fd, temporary = tempfile.mkstemp(prefix=".claude-pilot-", dir=self.home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(_json(config) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _install(self, credentials, config, previous_credentials, previous_config, check):
        check()
        self._set(NATIVE_SERVICE, self.username, _json(credentials) if credentials else None)
        try:
            check()
            self._write_config(config)
        except Exception:
            # Transaction rollback is local and immediate, unlike an explicit leave.
            # Do not overwrite a manual login that ignored native advisory locks.
            now_credentials, now_config = self._read()
            if (now_credentials == credentials and now_config == previous_config):
                self._set(NATIVE_SERVICE, self.username,
                          _json(previous_credentials) if previous_credentials else None)
            raise
        actual_credentials, actual_config = self._read()
        expected = self._fingerprint(credentials, config)
        if self._fingerprint(actual_credentials, actual_config) != expected:
            raise NativeDriftError("Native credentials changed during activation; the agent must pause.")
        return expected

    def _prepare(self, assignment, expected_fingerprint, initialize):
        """Prepare identical plan/apply state while the caller holds native locks."""
        self.preflight(assignment)
        try:
            token = assignment["access_token"]
            expiry = assignment["expires_at"]
            scopes = assignment["scopes"]
            account = assignment["account"]
            if not isinstance(token, str) or not token or len(token) > 65536:
                raise ValueError
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= self.clock() + 30:
                raise ValueError
            if not isinstance(scopes, list) or "user:inference" not in scopes or not all(isinstance(s, str) and s for s in scopes):
                raise ValueError
            if not isinstance(account, dict) or not all(isinstance(account.get(k), str) and account[k] for k in ("accountUuid", "emailAddress", "organizationUuid")):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise NativeStoreError("Assignment is missing valid native account/token metadata or expires too soon.") from None
        oauth = {"accessToken": token, "expiresAt": int(expiry * 1000), "scopes": scopes}
        for key in ("subscriptionType", "rateLimitTier"):
            if account.get(key) is not None:
                if not isinstance(account[key], str) or not account[key]:
                    raise NativeStoreError("Assignment has invalid subscription metadata.")
                oauth[key] = account[key]
        identity = {k: account[k] for k in ("accountUuid", "emailAddress", "organizationUuid")}
        credentials, config = self._read()
        current = self._fingerprint(credentials, config)
        if expected_fingerprint is None or expected_fingerprint != current:
            raise NativeDriftError("Native login changed; explicit enrollment is required before replacement.")
        backup = self._backup()
        if initialize:
            if backup is not None and backup.get("fingerprint") != current:
                raise NativeStoreError("A previous enrollment backup exists; restore or resolve it first.")
        elif backup is None:
            raise NativeStoreError("No enrollment backup exists; explicit initialization is required.")
        replacement = {k: v for k, v in credentials.items() if k in SHARED_KEYS}
        replacement["claudeAiOauth"] = oauth
        new_config = dict(config, oauthAccount=identity)
        # Check the complete serialized payload, including local MCP/plugin data,
        # before creating a backup or persisting a write journal. Never truncate
        # those fields to fit security(1)'s bounded interactive input buffer.
        validator = getattr(self.backend, "validate_password", None)
        if validator is not None:
            from .native_security import KeychainPayloadError
            try:
                validator(NATIVE_SERVICE, self.username, _json(replacement))
            except KeychainPayloadError:
                raise NativePayloadError("Native credential payload cannot be safely written by security(1); no data was changed.") from None
        return credentials, config, replacement, new_config, backup

    def plan(self, assignment: dict, expected_fingerprint: str | None, *, initialize=False) -> dict:
        """Return recovery fingerprints without writing credentials/config/backup.

        The usual temporary advisory locks are acquired. Applying this plan is
        still a separate compare-and-swap: planning grants no permission to
        overwrite a login that changes before ``apply`` acquires its locks.
        """
        with self._locked() as check:
            credentials, config, replacement, new_config, _ = self._prepare(
                assignment, expected_fingerprint, initialize)
            check()
            return {"before": self._fingerprint(credentials, config),
                    "keychain_only": self._fingerprint(replacement, config),
                    "after": self._fingerprint(replacement, new_config)}

    def apply(self, assignment: dict, expected_fingerprint: str | None, *, initialize=False) -> str:
        with self._locked() as check:
            credentials, config, replacement, new_config, backup = self._prepare(
                assignment, expected_fingerprint, initialize)
            if initialize and backup is None:
                backup = {"version": 1, "credentials": credentials,
                          "oauth_present": "oauthAccount" in config,
                          "oauthAccount": config.get("oauthAccount"),
                          "fingerprint": self._fingerprint(credentials, config)}
                self._set(PILOT_SERVICE, self.username + ":native-backup", _json(backup))
            return self._install(replacement, new_config, credentials, config, check)

    def restore(self, expected_fingerprint: str) -> str:
        """Explicitly restore displaced account state. Old grants may be revoked."""
        self.preflight()
        with self._locked() as check:
            credentials, config = self._read()
            if self._fingerprint(credentials, config) != expected_fingerprint:
                raise NativeDriftError("Native login changed; the old backup was not restored.")
            backup = self._backup()
            if backup is None:
                raise NativeStoreError("No enrollment backup exists.")
            self.preflight({"account": backup.get("oauthAccount") or {}})
            replacement = {k: v for k, v in backup["credentials"].items() if k not in SHARED_KEYS}
            replacement.update({k: v for k, v in credentials.items() if k in SHARED_KEYS})
            new_config = dict(config)
            if backup["oauth_present"]:
                new_config["oauthAccount"] = backup["oauthAccount"]
            else:
                new_config.pop("oauthAccount", None)
            fingerprint = self._install(replacement, new_config, credentials, config, check)
            self._set(PILOT_SERVICE, self.username + ":native-backup", None)
            return fingerprint
