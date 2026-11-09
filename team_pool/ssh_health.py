"""Read-only diagnosis of the native pool's macOS/SSH lifecycle boundary.

A GUI launchd domain, an active sync agent, and a readable Keychain in this
process do not establish access from Claude Desktop's remote helper. This
diagnostic deliberately leaves that separate user journey unverified.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import os
import re
import subprocess
import sys

from .client import SERVICE
from .native import NativeKeychainAccessError


_LAUNCHCTL_TIMEOUT = 3
_MAX_LAUNCHCTL_OUTPUT = 64 * 1024
_STATES = frozenset({"active", "enrolled", "degraded", "paused", "revoked", "left"})
_REASONS = frozenset({
    "keychain_access_required", "native_setup_or_policy", "native_credentials_changed",
    "native_store_capacity", "partial_native_write", "device_revoked",
    "coordinator_unavailable", "recovery_retained",
})
_LAUNCHD_STATES = frozenset({"running", "not running", "waiting", "spawn scheduled"})
_REMOTE_GUIDANCE = (
    "The remote helper has a separate macOS Keychain context. Retry the original "
    "remote conversation to verify it; this report does not test helper authentication."
)
_KEYCHAIN_GUIDANCE = (
    "In Terminal on the remote Mac's screen, as this same macOS user, resolve "
    "Keychain access, then run team-pool-client resume and team-pool-client sync --once. "
    "Do not reconnect pooled accounts to fix a local Keychain access failure."
)


def _known(value, choices, fallback="unknown"):
    return value if isinstance(value, str) and value in choices else fallback


def _timestamp(value):
    # State files are not trusted diagnostic output. Accept finite epoch seconds,
    # never an arbitrary date string, account label, URL, or exception text.
    if type(value) not in (int, float):
        return None
    try:
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value, timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _launchctl(target, *, runner, capture=False):
    try:
        result = runner(
            ["/bin/launchctl", "print", target], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=_LAUNCHCTL_TIMEOUT, check=False,
        )
        # launchctl distinguishes an absent domain (112) or service (113) from
        # other failures. A permission/usage error must not be called a logout.
        if result.returncode in (112, 113):
            return {"status": "missing"}
        if result.returncode != 0:
            return {"status": "probe_failed"}
        if not capture:
            return {"status": "present"}
        raw = result.stdout
        if not isinstance(raw, (bytes, str)) or len(raw) > _MAX_LAUNCHCTL_OUTPUT:
            return {"status": "unknown"}
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        # launchctl's service fields have one tab of indentation. Resource and
        # jetsam groups contain their own deeper `state = active` fields; those
        # are not the service's run state. Fail closed if the format changes or
        # more than one top-level state appears.
        states = re.findall(r"^\tstate = ([^\r\n]+?)\s*$", text, re.MULTILINE)
        state = _known(states[0], _LAUNCHD_STATES) if len(states) == 1 else "unknown"
        if state == "unknown":
            return {"status": "unknown"}
        return {"status": "running" if state == "running" else "stopped",
                "launchd_state": state}
    except subprocess.TimeoutExpired:
        return {"status": "probe_timeout"}
    except Exception:
        # launchctl output and exceptions can include paths, environment values,
        # or full arguments. None belongs in a shareable diagnosis.
        return {"status": "probe_failed"}


def doctor(client, *, runner=subprocess.run, platform=None, bridge_health=None):
    """Return a safe metadata report without changing enrollment or native state.

    Inspect only this process's UID; the console user may be a different logged-in
    user after Fast User Switching. State is atomically replaced by the client, so
    this read does not wait behind a sync mutation/network lock. Native access is
    attempted only after finding this UID's GUI domain and only with the native
    adapter's noninteractive guard. No provider or coordinator request is made.
    """
    result = {
        "version": 1,
        "read_only": True,
        "checked_at": _timestamp(client.clock()),
        "category": "unknown",
        "checks": {
            "gui_session": {"status": "not_checked"},
            "sync_service": {"status": "not_checked"},
            "client": {"status": "not_checked"},
            "assignment": {"status": "not_checked"},
            "caller_keychain": {"status": "not_checked"},
            "gui_bridge": {"status": "not_checked"},
            "remote_helper": {"status": "not_performed", "authenticated": None},
        },
        "guidance": [],
    }
    checks = result["checks"]

    def finish(category, *guidance):
        result["category"] = category
        result["guidance"] = list(guidance)
        return result

    if (sys.platform if platform is None else platform) != "darwin":
        return finish("unsupported_platform", "Run this diagnostic on the remote Mac as its enrolled macOS user.")
    uid = os.getuid()
    if uid == 0:
        return finish("wrong_user", "Run this diagnostic as the enrolled macOS user, without sudo.")

    # No console-user lookup: a switched-away GUI login is still the correct
    # launchd domain and may continue running its sync agent.
    gui = _launchctl(f"gui/{uid}", runner=runner)
    checks["gui_session"] = gui
    if gui["status"] == "missing":
        return finish(
            "gui_session_missing",
            "Sign into this same macOS user on the remote Mac's screen and stay logged in. "
            "SSH alone does not create its GUI sync session or unlock its login Keychain. "
            "Fast User Switching may leave other users logged in.",
            _REMOTE_GUIDANCE,
        )
    if gui["status"] != "present":
        return finish("gui_session_unknown", "Could not inspect this user's GUI login. Retry the diagnostic on the remote Mac's screen.")

    checks["sync_service"] = _launchctl(f"gui/{uid}/{SERVICE}", runner=runner, capture=True)
    try:
        state = client._load(optional=True)
    except Exception:
        return finish("client_state_unreadable", "Preserve the current enrollment and recovery files; inspect their ownership and client setup before retrying.")
    if state is None:
        checks["client"] = {"status": "not_enrolled"}
        return finish("not_enrolled", "Run the one-time pool setup in Terminal on the remote Mac's screen as this user.")
    if not isinstance(state, dict):
        return finish("client_state_unreadable", "Preserve the current enrollment and recovery files; inspect the client setup before retrying.")

    client_state = _known(state.get("state"), _STATES)
    checks["client"] = {"status": client_state}
    reason = _known(state.get("reason"), _REASONS)
    if client_state == "paused":
        checks["client"]["pause_reason"] = reason
    if "pending_write" in state:
        checks["client"]["write_recovery_pending"] = True
    synced_at = _timestamp(state.get("synced_at"))
    if synced_at is not None:
        checks["client"]["synced_at"] = synced_at
    expiry = _timestamp(state.get("expires_at"))
    now = client.clock()
    if expiry is None or _timestamp(now) is None:
        checks["assignment"] = {"status": "unknown"}
    else:
        checks["assignment"] = {
            "status": "expired" if state["expires_at"] <= now else "current",
            "expires_at": expiry,
        }

    if bridge_health is None:
        from .gui_exec import health
        bridge_health = health
    try:
        bridge = bridge_health(op="check_ready", timeout=3)
        ready = bridge.get("native_ready") is True and bridge.get("ok") is True and bridge.get("draining") is False
        checks["gui_bridge"] = {"status": "ready" if ready else "not_ready"}
    except Exception:
        ready = False
        checks["gui_bridge"] = {"status": "unavailable"}

    # A working GUI bridge intentionally removes the caller's SSH audit context
    # from native execution. Do not label its expected Keychain denial a failure.
    # Snapshot returns metadata, but internally reads native secrets. Never call
    # it outside the noninteractive guard or fall back to an interactive read.
    try:
        if ready:
            checks["caller_keychain"] = {"status": "not_required_gui_bridge"}
        else:
            with client.native.noninteractive():
                snapshot = client.native.snapshot()
            present = snapshot.get("has_credentials")
            checks["caller_keychain"] = {
                "status": "readable",
                "has_credentials": present if type(present) is bool else None,
            }
    except NativeKeychainAccessError:
        checks["caller_keychain"] = {"status": "access_required"}
    except Exception:
        checks["caller_keychain"] = {"status": "unavailable"}

    if client_state in ("revoked", "left"):
        return finish("enrollment_inactive", "This enrollment cannot sync. Follow the enrollment recovery instructions before setting up access again.")
    if client_state == "paused":
        if reason == "keychain_access_required":
            return finish("client_paused", _KEYCHAIN_GUIDANCE, _REMOTE_GUIDANCE)
        return finish(
            "client_paused",
            "Resolve the reported pause reason before explicitly resuming in Terminal on the remote Mac's screen. "
            "Do not overwrite a manual login or interrupted native write; retain recovery state.",
            _REMOTE_GUIDANCE,
        )
    if checks["client"].get("write_recovery_pending"):
        return finish("native_write_recovery_pending", "Preserve the recovery state and follow the interrupted-write recovery instructions before resuming.")
    if client_state == "unknown":
        return finish("client_state_unknown", "Preserve the current enrollment and inspect its client state before retrying.")
    service = checks["sync_service"]["status"]
    if service == "missing":
        return finish("sync_service_missing", "Run the pool setup in Terminal on the remote Mac's screen to restore this user's GUI sync service. Stay logged into this macOS user.", _REMOTE_GUIDANCE)
    if service == "stopped":
        return finish("sync_service_stopped", "In Terminal on the remote Mac's screen, inspect team-pool-client status and correct any reported condition. Run resume and sync --once, then restart the pool sync service.", _REMOTE_GUIDANCE)
    if service != "running":
        return finish("sync_service_unknown", "Could not verify the GUI sync service. Retry the diagnostic before changing enrollment or pooled accounts.")
    if checks["assignment"]["status"] == "expired":
        return finish("assignment_expired", "The local subscription assignment expired. Restore GUI background sync before retrying the original conversation.", _REMOTE_GUIDANCE)
    if ready:
        return finish("gui_bridge_ready_helper_unverified", "The GUI execution bridge can read the current native login. Retry a message in the original remote conversation to verify the complete path; keep this macOS user logged in.")
    if checks["gui_bridge"]["status"] == "not_ready":
        return finish("gui_bridge_pool_unavailable", "The GUI execution bridge is present but native pool readiness failed. In this user's desktop Terminal, inspect status and rerun pool setup after resolving the reported condition.", _REMOTE_GUIDANCE)
    if checks["caller_keychain"]["status"] == "access_required":
        return finish("caller_keychain_access_required", _KEYCHAIN_GUIDANCE, _REMOTE_GUIDANCE)
    if checks["caller_keychain"]["status"] != "readable":
        return finish("caller_keychain_unavailable", "The guarded native-store read failed. Inspect this user's local Keychain and native setup; this does not establish a pooled-account sign-in failure.", _REMOTE_GUIDANCE)
    if checks["caller_keychain"].get("has_credentials") is not True:
        return finish("native_credential_missing", "In Terminal on the remote Mac's screen, inspect team-pool-client status and run sync --once after resolving any ownership or setup condition.", _REMOTE_GUIDANCE)
    if checks["assignment"]["status"] == "expired":
        return finish("assignment_expired", "The recorded assignment expired. Run team-pool-client sync --once in Terminal on the remote Mac's screen and inspect any sync error. Keep this macOS user logged in for renewal.", _REMOTE_GUIDANCE)
    if checks["assignment"]["status"] != "current":
        return finish("assignment_unknown", "No valid assignment expiry could be verified. Inspect client status and complete native synchronization before retrying the remote conversation.", _REMOTE_GUIDANCE)
    return finish("local_checks_passed_helper_unverified", "This user's GUI sync service is running, the recorded assignment is current, and this caller can read native credentials.", _REMOTE_GUIDANCE)
