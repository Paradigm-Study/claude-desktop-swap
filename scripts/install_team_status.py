#!/usr/bin/env python3
"""Build, install, or remove the read-only Claude Swap menu bar on macOS."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


LABEL = "study.paradigm.claude-desktop-swap.status"
APP_NAME = "Claude Swap Status.app"
EXECUTABLE = "ClaudeSwapStatus"
MARKER = {"owner": LABEL, "format": 1}
SOURCE = Path(__file__).resolve().parent.parent / "macos/TeamPoolStatus.swift"


class InstallError(Exception):
    pass


def paths(home=None):
    home = Path(home or Path.home())
    return home / "Applications" / APP_NAME, home / "Library/LaunchAgents" / f"{LABEL}.plist"


def exists(path):
    return path.exists() or path.is_symlink()


def specification(app):
    return {"Label": LABEL, "ProgramArguments": [str(app / "Contents/MacOS" / EXECUTABLE)],
            "RunAtLoad": True, "ProcessType": "Interactive"}


def verify_app(app):
    if not exists(app):
        return
    try:
        if app.is_symlink() or not app.is_dir() or app.stat().st_uid != os.getuid():
            raise ValueError
        for folder, directories, files in os.walk(app):
            for name in directories + files:
                child = Path(folder) / name
                if child.is_symlink() or child.stat().st_uid != os.getuid():
                    raise ValueError
        marker = json.loads((app / "Contents/Resources/claude-swap-status.json").read_text())
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if (marker != MARKER or not isinstance(info, dict) or info.get("CFBundleIdentifier") != LABEL
                or info.get("CFBundleExecutable") != EXECUTABLE
                or not (app / "Contents/MacOS" / EXECUTABLE).is_file()):
            raise ValueError
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise InstallError(f"Refusing to change an unrecognized app: {app}") from None


def verify_agent(agent, app):
    if not exists(agent):
        return
    try:
        if agent.is_symlink() or not agent.is_file() or agent.stat().st_uid != os.getuid():
            raise ValueError
        if plistlib.loads(agent.read_bytes()) != specification(app):
            raise ValueError
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise InstallError(f"Refusing to change an unrecognized LaunchAgent: {agent}") from None


def launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=False)


def target():
    return f"gui/{os.getuid()}/{LABEL}"


def loaded():
    return launchctl("print", target()).returncode == 0


def stop():
    if not loaded():
        return
    launchctl("bootout", target())
    for _ in range(50):
        if not loaded():
            return
        time.sleep(0.1)
    raise InstallError("Menu bar is still stopping; retry after it exits.")


def build_bundle(destination):
    contents = destination / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    info = {"CFBundleIdentifier": LABEL, "CFBundleExecutable": EXECUTABLE,
            "CFBundleName": "Claude Swap Status", "CFBundleDisplayName": "Claude Swap",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1", "LSUIElement": True, "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    (contents / "Resources/claude-swap-status.json").write_text(json.dumps(MARKER) + "\n")
    try:
        subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", "-framework", "AppKit",
                        str(SOURCE), "-o", str(contents / "MacOS" / EXECUTABLE)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(destination)], check=True)
    except subprocess.CalledProcessError:
        raise InstallError("Menu bar build failed; the installed app was not changed.") from None


def install(home=None):
    app, agent = paths(home)
    verify_app(app)
    verify_agent(agent, app)
    if loaded() and not exists(agent):
        raise InstallError("This service label is already loaded without an owned LaunchAgent file.")
    app.parent.mkdir(parents=True, exist_ok=True)
    agent.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".claude-swap-status-", dir=app.parent) as temporary:
        stage = Path(temporary) / APP_NAME
        build_bundle(stage)
        # Recheck after compilation before changing either installed artifact.
        verify_app(app)
        verify_agent(agent, app)
        previous_plist = agent.read_bytes() if exists(agent) else None
        was_loaded = loaded()
        if was_loaded and previous_plist is None:
            raise InstallError("This service label appeared without an owned LaunchAgent file.")
        stop()
        # Keep rollback outside the temporary directory so even an interrupted
        # recovery cannot cause its cleanup to erase the previous installation.
        backup = app.parent / f".claude-swap-status-previous-{uuid.uuid4().hex}.app"
        if exists(app):
            app.rename(backup)
        try:
            stage.rename(app)
            descriptor, pending_name = tempfile.mkstemp(prefix=f".{LABEL}.", dir=agent.parent)
            pending = Path(pending_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(plistlib.dumps(specification(app)))
                pending.replace(agent)
            finally:
                pending.unlink(missing_ok=True)
            if launchctl("enable", target()).returncode:
                raise InstallError("Could not enable the menu bar LaunchAgent.")
            if launchctl("bootstrap", f"gui/{os.getuid()}", str(agent)).returncode:
                raise InstallError("Could not start the menu bar LaunchAgent.")
        except BaseException:
            try:
                stop()
            except InstallError:
                raise InstallError(f"Could not stop the new menu bar. Previous app retained at {backup}.") from None
            if exists(app):
                shutil.rmtree(app)
            if backup.exists():
                backup.rename(app)
            if previous_plist is None:
                agent.unlink(missing_ok=True)
            else:
                agent.write_bytes(previous_plist)
                agent.chmod(0o600)
                if was_loaded:
                    launchctl("bootstrap", f"gui/{os.getuid()}", str(agent))
            raise
        if backup.exists():
            shutil.rmtree(backup)
    return {"app": str(app), "launch_agent": str(agent), "loaded": loaded()}


def uninstall(home=None):
    app, agent = paths(home)
    verify_app(app)
    verify_agent(agent, app)
    if not exists(app) and not exists(agent):
        return {"installed": False}
    if loaded() and not exists(agent):
        raise InstallError("Refusing to stop a service without its owned LaunchAgent file.")
    stop()
    agent.unlink(missing_ok=True)
    if exists(app):
        shutil.rmtree(app)
    return {"installed": False}


def status(home=None):
    app, agent = paths(home)
    verify_app(app)
    verify_agent(agent, app)
    return {"app": str(app), "installed": exists(app), "launch_agent": exists(agent), "loaded": loaded()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "uninstall", "status"))
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.exit(1, "The menu bar requires macOS and Xcode Command Line Tools.\n")
    try:
        result = {"install": install, "uninstall": uninstall, "status": status}[args.command]()
    except (InstallError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
