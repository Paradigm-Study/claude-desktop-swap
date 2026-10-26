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
import stat
import sys
import tempfile
import threading
import time
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


class NativeDriftError(NativeStoreError):
    """Another writer changed the native account; do not overwrite it."""


class NativePolicyError(NativeStoreError):
    """An auth override or policy requires a different configuration."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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
        except Exception:
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
        except Exception:
            raise NativeStoreError("Keychain write failed or could not be verified.") from None

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

    def preflight(self, assignment: dict | None = None):
        if any(self.environ.get(key) for key in AUTH_ENV):
            raise NativePolicyError("Auth/provider/config-directory overrides are unsupported by the native pilot.")
        if self.claude.is_symlink() or self.config.is_symlink():
            raise NativePolicyError("Symlinked native configuration is unsupported by the pilot.")
        if os.path.lexists(self.claude / ".credentials.json"):
            raise NativePolicyError("A credential-file fallback exists; resolve it before enrolling.")
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
