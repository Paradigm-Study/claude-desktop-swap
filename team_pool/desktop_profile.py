"""Resolve Desktop's launcher policy without creating a different app identity.

Desktop reads its optional configLibrary and macOS managed preference domain.
Its ordinary config.json stores app state; it is an initialization marker here,
never a place to write the launcher flag.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import stat

FLAG = "claudeCodeProcessWrapperEnabled"
DOMAIN = "com.anthropic.claudefordesktop.plist"


class DesktopProfileError(Exception):
    """A safe, actionable Desktop layout diagnostic."""


@dataclass(frozen=True)
class DesktopTarget:
    kind: str
    path: Path
    profile_id: str | None = None


def configuration(path, *, owner, system=False, plist=False):
    """Read a bounded regular file without following any path-component link."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise DesktopProfileError("Desktop configuration paths must be absolute and contain no parent traversal.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    leaf = None
    try:
        for part in path.parts[1:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
            info = os.fstat(fd)
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if system and (info.st_uid not in (0, owner) or (info.st_mode & 0o022 and not sticky_root)):
                raise DesktopProfileError("Desktop managed preferences have unsafe directory ownership or permissions.")
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        info = os.fstat(leaf)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or (system and info.st_mode & 0o022) or info.st_size > 1024 * 1024):
            raise DesktopProfileError("Desktop configuration has unsafe ownership, permissions, or size.")
        with os.fdopen(leaf, "rb", closefd=False) as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        value = plistlib.loads(raw) if plist else json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise DesktopProfileError("Desktop configuration is unreadable, invalid, or reached through a symlink. Preserve it and resolve that condition before setup.") from None
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(fd)


def managed_path(*, uid=None, username=None, managed_root=None):
    uid = os.getuid() if uid is None else uid
    username = username or pwd.getpwuid(uid).pw_name
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", username) or username in (".", ".."):
        raise DesktopProfileError("The macOS account name cannot be used for a user-scoped Desktop policy.")
    root = Path(managed_root or "/Library/Managed Preferences")
    return root / username / DOMAIN


def resolve_target(home, *, uid=None, username=None, managed_root=None, system_uid=0):
    """Return the actual supported policy location; do not synthesize profiles."""
    uid = os.getuid() if uid is None else uid
    home = Path(home)
    if os.environ.get("CLAUDE_USER_DATA_DIR"):
        raise DesktopProfileError("A custom CLAUDE_USER_DATA_DIR is active. Use the ordinary Claude Desktop data folder before setup.")
    user_policy = managed_path(uid=uid, username=username, managed_root=managed_root)
    machine_policy = user_policy.parent.parent / DOMAIN
    policies = [(path, configuration(path, owner=system_uid, system=True, plist=True))
                for path in (machine_policy, user_policy)]
    for _, policy in policies:
        if policy is not None and FLAG in policy and policy[FLAG] is not True:
            raise DesktopProfileError("A managed Desktop policy disables the launcher. Ask its administrator to resolve that policy before setup.")
    # A prior standard-layout install remains owned by its recorded policy if
    # Desktop later creates a configuration library of its own.
    if policies[1][1] is not None and policies[1][1].get(FLAG) is True:
        return DesktopTarget("managed", user_policy)
    profiles = home / "Library/Application Support/Claude-3p/configLibrary"
    meta = configuration(profiles / "_meta.json", owner=uid)
    if meta is not None:
        selected = meta.get("appliedId")
        if not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", selected):
            raise DesktopProfileError("Claude Desktop's selected profile identifier is invalid. Select its existing profile in Desktop, then repeat setup.")
        if meta.get("hybridPointer"):
            raise DesktopProfileError("Claude Desktop is using a remotely managed profile. Select its local native profile before setup.")
        target = DesktopTarget("profile", profiles / (selected + ".json"), selected)
        if configuration(target.path, owner=uid) is None:
            raise DesktopProfileError("Claude Desktop's selected profile file is missing. Open Desktop and resolve that profile before setup.")
        return target
    # config.json is created by ordinary Desktop even when the optional
    # configuration editor has never been opened. Only inspect its existence
    # and integrity; never alter it or relocate its conversation data.
    marker = home / "Library/Application Support/Claude/config.json"
    if configuration(marker, owner=uid) is None:
        raise DesktopProfileError("Open Claude Desktop from Applications, finish its first sign-in, then run this setup command again. Its ordinary app data has not been initialized; no profile needs to be created.")
    if any(policy and set(policy) - {FLAG} for _, policy in policies):
        raise DesktopProfileError("This Mac already has managed Claude Desktop settings. Ask its administrator to enable the native launcher; setup will not change those policies.")
    return DesktopTarget("managed", user_policy)


def launcher_enabled(target, *, uid=None, system_uid=0):
    uid = os.getuid() if uid is None else uid
    managed = target.kind == "managed"
    value = configuration(target.path, owner=system_uid if managed else uid,
                          system=managed, plist=managed)
    return value is not None and value.get(FLAG) is True
