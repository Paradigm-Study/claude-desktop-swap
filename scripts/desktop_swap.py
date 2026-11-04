#!/usr/bin/env python3
"""Install the experimental native-subscription launcher for Claude Desktop.

The machine-wide launcher setting can also affect SSH Code sessions. Each
enrolled macOS user needs accessible native credentials in the session's launch
context. This does not manage accounts or enable paid Fast mode. Quit Claude
Desktop before installing or uninstalling for that user.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import pwd
import plistlib
import re
import stat
import subprocess
import sys
import uuid

# This script is also run directly by the privileged installer from its pinned
# source tree. Use the matching helper, not an older globally installed client.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from team_pool.desktop_profile import (DesktopProfileError, DesktopTarget,
                                       configuration, managed_path, resolve_target)


FLAG = "claudeCodeProcessWrapperEnabled"
STATE_NAME = "claude-desktop-swap-state.json"
LAUNCHER_NAME = "native-subscription-launcher"
LOCK_NAME = ".claude-desktop-swap.lock"
MAX_LAUNCHER_BYTES = 16384  # Older pool clients read at most this many bytes.
ENV_TO_CLEAR = (
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)


class SetupError(Exception):
    """A precondition failed; the CLI prints only this safe diagnostic."""


@dataclass(frozen=True)
class Layout:
    home: Path
    uid: int
    gid: int
    system_dir: Path
    system_uid: int = 0
    system_gid: int = 0
    managed_root: Path = Path("/Library/Managed Preferences")
    username: str | None = None

    @property
    def profiles(self):
        return self.home / "Library/Application Support/Claude-3p/configLibrary"

    @property
    def managed(self):
        return self.system_dir / "managed-settings.json"

    @property
    def launcher(self):
        return self.system_dir / LAUNCHER_NAME

    @property
    def state(self):
        return self.system_dir / STATE_NAME


@dataclass(frozen=True)
class Snapshot:
    data: bytes
    mode: int
    uid: int
    gid: int


@contextmanager
def directory_fd(path: Path):
    """Open every path component without following symlinks, including parents."""
    if not path.is_absolute():
        raise SetupError("Expected an absolute configuration path.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if component in (".", ".."):
                raise SetupError("Unsafe configuration path.")
            new_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = new_fd
        yield fd
    finally:
        os.close(fd)


def read_file(path: Path) -> Snapshot | None:
    try:
        with directory_fd(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise SetupError(f"Expected a regular file: {path.name}")
                if info.st_size > 16 * 1024 * 1024:
                    raise SetupError(f"Configuration file is unexpectedly large: {path.name}")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read()
                return Snapshot(data, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None


def read_json(path: Path, *, required=False):
    snapshot = read_file(path)
    if snapshot is None:
        if required:
            raise SetupError(f"Missing {path.name}; open Claude Desktop and select a profile first.")
        return {}, None
    try:
        value = json.loads(snapshot.data)
    except (ValueError, UnicodeDecodeError):
        raise SetupError(f"Invalid JSON in {path.name}; nothing changed.") from None
    if not isinstance(value, dict):
        raise SetupError(f"Expected a JSON object in {path.name}; nothing changed.")
    return value, snapshot


def encode_json(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def profile_path(layout: Layout, profile_id):
    if not isinstance(profile_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", profile_id):
        raise SetupError("The selected Claude Desktop profile identifier is invalid.")
    return layout.profiles / f"{profile_id}.json"


def desktop_target(layout):
    try:
        return resolve_target(layout.home, uid=layout.uid, username=layout.username,
                              managed_root=layout.managed_root, system_uid=layout.system_uid)
    except DesktopProfileError as error:
        raise SetupError(str(error)) from None


def state_target(layout, state):
    if state.get("schema") == 2 or state.get("target_kind") == "managed":
        if state.get("target_kind") != "managed" or state.get("profile_id") is not None:
            raise SetupError("The installation target is invalid; nothing changed.")
        return DesktopTarget("managed", managed_path(uid=layout.uid, username=layout.username,
                                                    managed_root=layout.managed_root))
    return DesktopTarget("profile", profile_path(layout, state["profile_id"]), state["profile_id"])


def read_target(layout, target, *, required=False):
    if target.kind == "profile":
        value, snapshot = read_json(target.path, required=required)
        if snapshot is not None and snapshot.uid != layout.uid:
            raise SetupError("The Desktop profile is not owned by the target user; nothing changed.")
        return value, snapshot
    try:
        configuration(target.path, owner=layout.system_uid, system=True, plist=True)
    except DesktopProfileError as error:
        raise SetupError(str(error)) from None
    snapshot = read_file(target.path)
    if snapshot is None:
        if required:
            raise SetupError("The installed Desktop launcher policy is missing; preserve recovery state and inspect it before setup.")
        return {}, None
    if snapshot.uid != layout.system_uid or snapshot.mode & 0o022:
        raise SetupError("The Desktop launcher policy has unsafe ownership or permissions.")
    try:
        value = plistlib.loads(snapshot.data)
        if not isinstance(value, dict):
            raise ValueError
    except (ValueError, plistlib.InvalidFileException):
        raise SetupError("The Desktop launcher policy is invalid; nothing changed.") from None
    return value, snapshot


def encode_target(target, value, before):
    if target.kind == "profile":
        return encode_json(value)
    form = plistlib.FMT_BINARY if before and before.data.startswith(b"bplist") else plistlib.FMT_XML
    return plistlib.dumps(value, fmt=form, sort_keys=False)


def target_owner(layout, target):
    return (layout.system_uid, layout.system_gid) if target.kind == "managed" else (layout.uid, layout.gid)


def make_policy_directory(layout, target):
    if target.kind != "managed":
        return
    # Never create arbitrary parents or follow a replacement symlink. /Library
    # already exists on a real Mac; only the managed domain and user directory
    # may need creation on an unmanaged fresh installation.
    for path in (layout.managed_root, target.path.parent):
        created = False
        with directory_fd(path.parent) as parent:
            try:
                os.mkdir(path.name, 0o755, dir_fd=parent)
                created = True
            except FileExistsError:
                pass
        with directory_fd(path) as directory:
            info = os.fstat(directory)
            if info.st_uid != layout.system_uid or info.st_mode & 0o022:
                raise SetupError("The Desktop managed policy directory has unsafe ownership or permissions.")
            if created:
                # mkdir's requested mode is reduced by the caller's umask.
                # Desktop must be able to traverse these public policy paths.
                os.fchmod(directory, 0o755)


def render_launcher(uid: int, restore_fast_control=False) -> bytes:
    return render_users_launcher([{"uid": uid, "restore_fast_control": restore_fast_control}])


def render_users_launcher(users) -> bytes:
    lines = [
        "#!/bin/sh",
        "# Generated by claude-desktop-swap. Other users keep their existing environment.",
    ]
    for user in sorted(users, key=lambda value: value["uid"]):
        uid = user["uid"]
        if type(uid) is not int or uid <= 0 or type(user["restore_fast_control"]) is not bool:
            raise SetupError("The target must be a non-root macOS user with a valid Fast-mode option.")
        names = ENV_TO_CLEAR + (("CLAUDE_CODE_DISABLE_FAST_MODE",) if user["restore_fast_control"] else ())
        lines.extend([
            f'if [ "$(/usr/bin/id -u)" = "{uid}" ]; then',
            *(f"    unset {name}" for name in names),
            "fi",
        ])
    lines.extend(['exec "$@"', ""])
    result = "\n".join(lines).encode()
    if len(result) > MAX_LAUNCHER_BYTES:
        raise SetupError("Too many enrolled macOS users for the native launcher; nothing changed.")
    return result


def wrapper_setting(layout):
    # Desktop expects a JSON-encoded string, not a JSON array value.
    return json.dumps([str(layout.launcher)])


def original_value(document, key):
    return {"present": key in document, "value": document.get(key)}


def restore_value(document, key, original):
    if original["present"]:
        document[key] = original["value"]
    else:
        document.pop(key, None)


def write_file(path: Path, data: bytes | None, mode: int, owner: tuple[int, int]):
    """Atomically replace a leaf using a retained, symlink-free directory handle."""
    with directory_fd(path.parent) as parent:
        if data is None:
            try:
                os.unlink(path.name, dir_fd=parent)
            except FileNotFoundError:
                pass
            return
        temporary = f".claude-desktop-swap-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent,
        )
        try:
            if os.geteuid() == 0:
                os.fchown(fd, *owner)
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def apply_changes(changes, *, guards=()):
    """Check all inputs before writing, and roll back ordinary write failures."""
    changed_paths = {path for path, *_ in changes}
    guards = [(path, before) for path, before in guards if path not in changed_paths]

    def verify_guards():
        for path, before in guards:
            if read_file(path) != before:
                raise SetupError(f"{path.name} changed during setup; preserve recovery state and retry.")

    verify_guards()
    for path, before, _, _, _ in changes:
        if read_file(path) != before:
            raise SetupError(f"{path.name} changed during setup; retry after quitting Claude Desktop.")
    applied = []
    try:
        for path, before, after, mode, owner in changes:
            verify_guards()
            if read_file(path) != before:
                raise SetupError(f"{path.name} changed during setup; aborting.")
            write_file(path, after, mode, owner)
            applied.append((path, before, after))
    except BaseException:
        for path, before, after in reversed(applied):
            current = read_file(path)
            if (current.data if current else None) != after:
                raise SetupError(f"{path.name} changed during rollback; manual review is required.")
            write_file(
                path, before.data if before else None,
                before.mode if before else 0o600,
                (before.uid, before.gid) if before else (os.geteuid(), os.getegid()),
            )
        raise


def change(path, before, after, *, owner, mode=0o644):
    return (path, before, after, before.mode if before else mode, owner)


def user_records(state):
    return state["users"] if state["schema"] == 3 else [state]


def installed_user(layout, state):
    return next((user for user in user_records(state) if user["uid"] == layout.uid), None)


def user_layout(layout, user):
    """Resolve recorded identities against macOS, never accept a different home."""
    uid, home = user.get("uid"), user.get("home")
    if (type(uid) is not int or uid <= 0 or not isinstance(home, str)
            or not Path(home).is_absolute() or ".." in Path(home).parts):
        raise SetupError("The installation user identity is invalid; nothing changed.")
    if uid == layout.uid:
        candidate = layout
    else:
        try:
            account = pwd.getpwuid(uid)
        except KeyError:
            raise SetupError("An installed macOS account no longer exists; preserve recovery state for administrator review.") from None
        candidate = Layout(Path(account.pw_dir), uid, account.pw_gid, layout.system_dir,
                           layout.system_uid, layout.system_gid, layout.managed_root, account.pw_name)
    if (home != str(candidate.home)
            or ("username" in user and user["username"] != (candidate.username or pwd.getpwuid(uid).pw_name))
            or ("gid" in user and (type(user["gid"]) is not int or user["gid"] != candidate.gid))):
        raise SetupError("An installed macOS account identity changed; refusing to use its recorded paths.")
    return candidate


def validate_original(value):
    if not isinstance(value, dict) or type(value.get("present")) is not bool or "value" not in value:
        raise SetupError("The installation state is invalid; nothing changed.")


def load_state(layout):
    state, snapshot = read_json(layout.state, required=True)
    if snapshot.uid != layout.system_uid or snapshot.mode & 0o077:
        raise SetupError("The installation state has unsafe ownership or permissions.")
    if type(state.get("schema")) is not int or state["schema"] not in (1, 2, 3):
        raise SetupError("The installation uses an unsupported state format.")
    validate_original(state.get("managed_original"))
    for key in ("managed_existed", "launcher_existed"):
        if type(state.get(key)) is not bool:
            raise SetupError("The installation state is invalid; nothing changed.")
    if state["launcher_existed"]:
        mode = state.get("launcher_original_mode")
        if type(mode) is not int or mode < 0 or mode > 0o7777 or mode & 0o022:
            raise SetupError("The installation state contains an invalid file mode.")
    if state["schema"] == 3:
        if (not isinstance(state.get("users"), list) or not state["users"]
                or len(state["users"]) > 128):
            raise SetupError("The installation user records are invalid; nothing changed.")
        original = state.get("launcher_original_user")
        if state["launcher_existed"]:
            if not isinstance(original, dict) or set(original) != {"uid", "restore_fast_control"}:
                raise SetupError("The original launcher recovery state is invalid; nothing changed.")
            render_users_launcher([original])
        elif original is not None:
            raise SetupError("The original launcher recovery state is invalid; nothing changed.")
    uids, homes, paths = set(), set(), set()
    for user in user_records(state):
        if not isinstance(user, dict) or "profile_id" not in user or type(user.get("restore_fast_control")) is not bool:
            raise SetupError("The installation user record is incomplete; nothing changed.")
        if state["schema"] == 3 and (not {"username", "gid"}.issubset(user)
                                    or user.get("target_kind") not in ("managed", "profile")):
            raise SetupError("The installation target is invalid; nothing changed.")
        validate_original(user.get("profile_original"))
        owner = user_layout(layout, user)
        target = state_target(owner, user)
        if target.kind == "managed" and type(user.get("policy_existed")) is not bool:
            raise SetupError("The installation state is incomplete; nothing changed.")
        if owner.uid in uids or owner.home in homes or target.path in paths:
            raise SetupError("The installation contains conflicting user records; nothing changed.")
        uids.add(owner.uid)
        homes.add(owner.home)
        paths.add(target.path)
    return state, snapshot


def shared_state(layout, state):
    """Migrate only validated legacy ownership; the original machine backup stays immutable."""
    if state["schema"] == 3:
        return dict(state, users=list(state["users"]))
    owner = user_layout(layout, state)
    user = {key: state[key] for key in ("uid", "home", "profile_id", "profile_original", "restore_fast_control")}
    user.update(username=owner.username or pwd.getpwuid(owner.uid).pw_name, gid=owner.gid,
                target_kind=state_target(owner, state).kind)
    if user["target_kind"] == "managed":
        user["policy_existed"] = state["policy_existed"]
    return {
        "schema": 3,
        "managed_existed": state["managed_existed"],
        "managed_original": state["managed_original"],
        "launcher_existed": state["launcher_existed"],
        "launcher_original_mode": state.get("launcher_original_mode"),
        "launcher_original_user": ({"uid": owner.uid, "restore_fast_control": state["restore_fast_control"]}
                                   if state["launcher_existed"] else None),
        "users": [user],
    }


def verify_installation(layout, state, *, guards=None):
    managed, managed_snapshot = read_json(layout.managed, required=True)
    if managed_snapshot.uid != layout.system_uid or managed_snapshot.mode & 0o022:
        raise SetupError("The managed settings have unsafe ownership or permissions.")
    launcher = read_file(layout.launcher)
    expected = render_users_launcher(user_records(state))
    if managed.get("processWrapper") != wrapper_setting(layout):
        raise SetupError("The managed processWrapper changed; refusing to overwrite it.")
    if launcher is None or launcher.data != expected or launcher.mode != 0o755:
        raise SetupError("The installed launcher changed; refusing to overwrite it.")
    if launcher.uid != layout.system_uid:
        raise SetupError("The installed launcher has unsafe ownership.")
    if guards is not None:
        guards.extend(((layout.managed, managed_snapshot), (layout.launcher, launcher)))
    selected = (None, None, None)
    for user in user_records(state):
        owner = user_layout(layout, user)
        target = state_target(owner, user)
        config, profile_snapshot = read_target(owner, target, required=True)
        if config.get(FLAG) is not True:
            raise SetupError("The installed profile's launcher flag changed; refusing to overwrite it.")
        if guards is not None:
            guards.append((target.path, profile_snapshot))
        if owner.uid == layout.uid:
            selected = (target.path, config, profile_snapshot)
    return managed, managed_snapshot, *selected, launcher


def install(layout: Layout, restore_fast_control=False):
    if read_file(layout.state) is not None:
        state, state_snapshot = load_state(layout)
        guards = []
        *_, launcher = verify_installation(layout, state, guards=guards)
        user = installed_user(layout, state)
        if user is not None:
            if desktop_target(layout).path != state_target(layout, user).path:
                raise SetupError("A different Desktop profile is selected. Uninstall first, then install for the selected profile.")
            if user["restore_fast_control"] != restore_fast_control:
                raise SetupError("Uninstall first before changing the Fast-mode control option.")
            return "Already installed for this user. Restart Claude Desktop to load the launcher."
        target = desktop_target(layout)
        config, config_snapshot = read_target(layout, target, required=target.kind == "profile")
        state = shared_state(layout, state)
        if any(user["home"] == str(layout.home)
               or state_target(user_layout(layout, user), user).path == target.path for user in state["users"]):
            raise SetupError("The new user conflicts with an installed configuration; nothing changed.")
        user = {
            "uid": layout.uid, "gid": layout.gid, "home": str(layout.home),
            "username": layout.username or pwd.getpwuid(layout.uid).pw_name,
            "target_kind": target.kind, "profile_id": target.profile_id,
            "profile_original": original_value(config, FLAG),
            "restore_fast_control": restore_fast_control,
        }
        if target.kind == "managed":
            user["policy_existed"] = config_snapshot is not None
        state["users"].append(user)
        expected_launcher = render_users_launcher(state["users"])
        config[FLAG] = True
        make_policy_directory(layout, target)
        system_owner = (layout.system_uid, layout.system_gid)
        apply_changes([
            (layout.launcher, launcher, expected_launcher, 0o755, system_owner),
            change(target.path, config_snapshot, encode_target(target, config, config_snapshot), owner=target_owner(layout, target)),
            (layout.state, state_snapshot, encode_json(state), 0o600, system_owner),
        ], guards=guards)
        return "Installed for this user; other enrolled macOS users were retained. Restart Claude Desktop to load the launcher."

    target = desktop_target(layout)
    config, config_snapshot = read_target(layout, target, required=target.kind == "profile")
    managed, managed_snapshot = read_json(layout.managed)
    if managed_snapshot is not None and (
        managed_snapshot.uid != layout.system_uid or managed_snapshot.mode & 0o022
    ):
        raise SetupError("The existing managed settings have unsafe ownership or permissions.")
    expected_setting = wrapper_setting(layout)
    if "processWrapper" in managed and managed["processWrapper"] != expected_setting:
        raise SetupError("An existing managed processWrapper conflicts with this integration; nothing changed.")
    launcher = read_file(layout.launcher)
    expected_launcher = render_launcher(layout.uid, restore_fast_control)
    if launcher is not None and launcher.data != expected_launcher:
        raise SetupError("An existing launcher conflicts with this integration; nothing changed.")
    if launcher is not None and (launcher.uid != layout.system_uid or launcher.mode & 0o022):
        raise SetupError("The existing launcher has unsafe ownership or permissions.")

    state = {
        "schema": 1 if target.kind == "profile" else 2, "uid": layout.uid, "home": str(layout.home),
        "profile_id": target.profile_id,
        "restore_fast_control": restore_fast_control,
        "managed_existed": managed_snapshot is not None,
        "managed_original": original_value(managed, "processWrapper"),
        "profile_original": original_value(config, FLAG),
        "launcher_existed": launcher is not None,
        "launcher_original_mode": launcher.mode if launcher else None,
    }
    if target.kind == "managed":
        state.update(target_kind="managed", policy_existed=config_snapshot is not None)
    managed["processWrapper"] = expected_setting
    config[FLAG] = True
    system_owner = (layout.system_uid, layout.system_gid)
    make_policy_directory(layout, target)
    apply_changes([
        (layout.launcher, launcher, expected_launcher, 0o755, system_owner),
        change(layout.managed, managed_snapshot, encode_json(managed), owner=system_owner),
        change(target.path, config_snapshot, encode_target(target, config, config_snapshot), owner=target_owner(layout, target)),
        (layout.state, None, encode_json(state), 0o600, system_owner),
    ])
    return "Installed. Start Claude Desktop and resume an existing local Code conversation."


def uninstall(layout: Layout):
    if read_file(layout.state) is None:
        raise SetupError("No installation state found; nothing changed.")
    state, state_snapshot = load_state(layout)
    user = installed_user(layout, state)
    if user is None:
        raise SetupError("This macOS user is not installed; other users' installations were retained.")
    guards = []
    managed, managed_snapshot, profile, config, config_snapshot, launcher = verify_installation(layout, state, guards=guards)
    target = state_target(layout, user)
    restore_value(config, FLAG, user["profile_original"])
    system_owner = (layout.system_uid, layout.system_gid)
    config_data = (None if target.kind == "managed" and not config and not user["policy_existed"]
                   else encode_target(target, config, config_snapshot))
    remaining = [record for record in user_records(state) if record["uid"] != layout.uid]
    if remaining:
        state = dict(state, users=remaining)
        apply_changes([
            change(profile, config_snapshot, config_data, owner=target_owner(layout, target)),
            (layout.launcher, launcher, render_users_launcher(remaining), 0o755, system_owner),
            (layout.state, state_snapshot, encode_json(state), 0o600, system_owner),
        ], guards=guards)
        return "Uninstalled for this user. Other enrolled macOS users retain their native launcher."
    restore_value(managed, "processWrapper", state["managed_original"])
    managed_data = encode_json(managed) if managed or state["managed_existed"] else None
    launcher_data = ((render_users_launcher([state["launcher_original_user"]]) if state["schema"] == 3 else launcher.data)
                     if state["launcher_existed"] else None)
    original_mode = state.get("launcher_original_mode") if state["launcher_existed"] else 0o755
    if not isinstance(original_mode, int) or original_mode < 0 or original_mode > 0o7777:
        raise SetupError("The installation state contains an invalid file mode.")
    apply_changes([
        change(profile, config_snapshot, config_data, owner=target_owner(layout, target)),
        change(layout.managed, managed_snapshot, managed_data, owner=system_owner),
        (layout.launcher, launcher, launcher_data, original_mode, system_owner),
        (layout.state, state_snapshot, None, 0o600, system_owner),
    ], guards=guards)
    return "Uninstalled. Start Claude Desktop to use its original authentication behavior."


def status(layout: Layout):
    if read_file(layout.state) is None:
        return "Not installed by this tool."
    state, _ = load_state(layout)
    verify_installation(layout, state)
    user = installed_user(layout, state)
    if user is None:
        return "Not installed for this macOS user. Other enrolled users' installation files verified."
    selected = desktop_target(layout).path == state_target(layout, user).path
    fast = "restored (paid usage is still separate)" if user["restore_fast_control"] else "unchanged"
    return ("Installation files verified. "
            + ("The installed profile is selected. " if selected else "WARNING: a different Desktop profile is selected. ")
            + f"Fast-mode control: {fast}. This does not verify a running session's account.")


def make_system_directory(layout):
    # The parent must already exist; refuse every symlink component.
    created = False
    with directory_fd(layout.system_dir.parent) as parent:
        try:
            os.mkdir(layout.system_dir.name, 0o755, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
    with directory_fd(layout.system_dir) as directory:
        info = os.fstat(directory)
        if info.st_uid != layout.system_uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise SetupError("The system configuration directory has unsafe ownership or permissions.")
        if created:
            os.fchmod(directory, 0o755)


def repair_access(layout):
    """Repair only public directory access demonstrably broken by this tool.

    a7 could leave an inaccessible directory and lock before profile discovery.
    a8 could finish writing a recognized installation below inaccessible policy
    directories. Read all ownership evidence as root before changing any mode;
    never infer ownership merely from a familiar pathname.
    """
    refusal = ("Cannot safely repair this Desktop installation's directory access. "
               "Preserve its files and ask an administrator to inspect ownership and contents; nothing was widened.")
    with ExitStack() as stack:
        try:
            system_fd = stack.enter_context(directory_fd(layout.system_dir))
        except FileNotFoundError:
            return "No installer directory exists; no access repair was needed."

        def validate_directory(fd):
            info = os.fstat(fd)
            if info.st_uid != layout.system_uid or stat.S_IMODE(info.st_mode) & 0o022:
                raise SetupError(refusal)
            return info

        def public(fd):
            return stat.S_IMODE(os.fstat(fd).st_mode) & 0o055 == 0o055

        validate_directory(system_fd)
        entries = set(os.listdir(system_fd))
        if LOCK_NAME in entries:
            lock = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=system_fd)
        elif not entries:
            lock = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=system_fd)
            os.fchmod(lock, 0o600)
        else:
            raise SetupError(refusal)
        stack.callback(os.close, lock)
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != layout.system_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise SetupError(refusal)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("Another installer is running; retry access repair after it finishes.") from None

        entries = set(os.listdir(system_fd))
        repairs = [system_fd] if not public(system_fd) else []
        expected = {LOCK_NAME}
        if entries != expected:
            expected |= {STATE_NAME, LAUNCHER_NAME, "managed-settings.json"}
            if entries != expected:
                raise SetupError(refusal)
            state, _ = load_state(layout)
            managed, managed_snapshot, *_ = verify_installation(layout, state)
            if (state["managed_existed"] or state["launcher_existed"]
                    or managed != {"processWrapper": wrapper_setting(layout)}
                    or managed_snapshot.mode != 0o644):
                raise SetupError(refusal)
            policy_users = set()
            root_fd = None
            for user in user_records(state):
                owner = user_layout(layout, user)
                target = state_target(owner, user)
                if target.kind != "managed":
                    continue
                config, config_snapshot = read_target(owner, target, required=True)
                if user["policy_existed"] or config != {FLAG: True} or config_snapshot.mode != 0o644:
                    raise SetupError(refusal)
                policy_fd = stack.enter_context(directory_fd(target.path.parent))
                if root_fd is None:
                    root_fd = stack.enter_context(directory_fd(layout.managed_root))
                validate_directory(policy_fd)
                validate_directory(root_fd)
                policy_users.add(target.path.parent.name)
                if set(os.listdir(policy_fd)) != {target.path.name}:
                    raise SetupError(refusal)
                if not public(policy_fd):
                    repairs.append(policy_fd)
            if root_fd is not None and not public(root_fd):
                # This domain is normally shared with other managed apps. Only
                # the exact tree recorded by this installer proves ownership.
                if set(os.listdir(root_fd)) != policy_users:
                    raise SetupError(refusal)
                repairs.append(root_fd)

        # Recheck the locked system directory immediately before mode changes.
        if set(os.listdir(system_fd)) != expected:
            raise SetupError(refusal)
        retained = os.fstat(lock)
        named = os.stat(LOCK_NAME, dir_fd=system_fd, follow_symlinks=False)
        if ((retained.st_dev, retained.st_ino) != (named.st_dev, named.st_ino)
                or not stat.S_ISREG(named.st_mode) or named.st_uid != layout.system_uid
                or stat.S_IMODE(named.st_mode) != 0o600 or named.st_nlink != 1):
            raise SetupError(refusal)
        for fd in repairs:
            validate_directory(fd)
        for fd in repairs:
            os.fchmod(fd, 0o755)
        return ("Repaired public launcher directory access; private files were unchanged."
                if repairs else "Launcher directory access is already readable; no modes were changed.")


@contextmanager
def installation_lock(layout):
    with directory_fd(layout.system_dir) as parent:
        fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != layout.system_uid:
                raise SetupError("The installer lock is unsafe.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError:
            raise SetupError("Another installer is running; retry after it finishes.") from None
        finally:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "status", "uninstall", "preflight", "repair-access"):
        sub = commands.add_parser(command)
        sub.add_argument("--user", required=True, help="macOS account that runs Claude Desktop")
        if command == "install":
            sub.add_argument("--restore-fast-control", action="store_true", help="remove Desktop's Fast-mode disable flag; Fast uses separate paid credits")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise SetupError("This integration supports macOS only.")
        try:
            user = pwd.getpwnam(args.user)
        except KeyError:
            raise SetupError("The requested macOS user does not exist.") from None
        if user.pw_uid == 0:
            raise SetupError("Choose the non-root macOS account that runs Claude Desktop.")
        layout = Layout(Path(user.pw_dir), user.pw_uid, user.pw_gid, Path("/Library/Application Support/ClaudeCode"), username=user.pw_name)
        if args.command == "preflight":
            target = desktop_target(layout)
            print("Desktop configuration is initialized; native launcher policy target: " + target.kind + ".")
            return 0
        if args.command == "status":
            # State is private and root-owned. Status requires sudo after installation.
            if os.geteuid() != 0 and layout.state.exists():
                raise SetupError("Run status with sudo to read the private installation state.")
            print(status(layout))
            return 0
        if os.geteuid() != 0:
            raise SetupError("Run installation or removal with sudo.")
        if args.command == "repair-access":
            print(repair_access(layout))
            return 0
        running = subprocess.run(["/usr/bin/pgrep", "-u", str(layout.uid), "-x", "Claude"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if running.returncode == 0:
            raise SetupError("Quit Claude Desktop first so it cannot overwrite its profile during setup.")
        if running.returncode not in (0, 1):
            raise SetupError("Could not check whether Claude Desktop is running; nothing changed.")
        make_system_directory(layout)
        with installation_lock(layout):
            result = install(layout, args.restore_fast_control) if args.command == "install" else uninstall(layout)
        print(result)
        return 0
    except SetupError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        # Never dump JSON contents or credentials on an I/O error.
        print(f"Error: configuration access failed ({error.strerror}); check permissions and symlinks.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
