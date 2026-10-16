#!/usr/bin/env python3
"""Opt-in, exact-version stale-quota correction for claude-swap 0.26.0.

Run with the Python interpreter that owns the cswap installation. This script
does not install dependencies, contact an API, or access account credentials.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

SUPPORTED_VERSION = "0.26.0"
ORIGINAL_SHA256 = "b59335259b6d99e3601ecc912084189c0e9b68d9f4e501afaa5dfaf0a8978ef7"
PATCHED_SHA256 = "c2851973ca98f85509d9a5a80bba226462bdc9bbcce6c647b08611c31ea8772b"
DEFAULT_PATCH = Path(__file__).resolve().parents[1] / "patches" / "claude-swap-0.26.0-stale-quota.patch"
BACKUP_SUFFIX = ".desktop-swap-0.26.0.original"


class PatchError(Exception):
    """A guard failed; the installation must not be overwritten."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_bytes(source: bytes, patch: bytes) -> bytes:
    """Apply this unified diff with exact contexts, no offsets or fuzzy match."""
    if sha256(source) != ORIGINAL_SHA256:
        raise PatchError("The source hash is not the supported original.")
    lines = source.decode("utf-8").splitlines(keepends=True)
    changes = patch.decode("utf-8").splitlines(keepends=True)
    if changes[:2] != ["--- a/claude_swap/autoswitch.py\n", "+++ b/claude_swap/autoswitch.py\n"]:
        raise PatchError("Unexpected patch target.")
    output, cursor, i = [], 0, 2
    while i < len(changes):
        match = re.fullmatch(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@\n", changes[i])
        if not match:
            raise PatchError("Invalid patch hunk.")
        start, old_count, new_start, new_count = map(int, match.groups())
        if start - 1 < cursor or start - 1 > len(lines):
            raise PatchError("Invalid patch position.")
        output.extend(lines[cursor:start - 1])
        cursor, removed, added = start - 1, 0, 0
        if len(output) != new_start - 1:
            raise PatchError("Invalid patch output position.")
        i += 1
        while i < len(changes) and not changes[i].startswith("@@ "):
            change = changes[i]
            if change[0] in " -":
                if cursor >= len(lines) or lines[cursor] != change[1:]:
                    raise PatchError("Patch context does not match.")
                cursor += 1
                removed += 1
            if change[0] in " +":
                output.append(change[1:])
                added += 1
            if change[0] not in " +-":
                raise PatchError("Invalid patch line.")
            i += 1
        if (removed, added) != (old_count, new_count):
            raise PatchError("Patch hunk length does not match.")
    output.extend(lines[cursor:])
    result = "".join(output).encode("utf-8")
    if sha256(result) != PATCHED_SHA256:
        raise PatchError("Patched output hash does not match the reviewed correction.")
    return result


def read_regular(path: Path) -> bytes:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise PatchError(f"Refusing nonregular file: {path}")
    return path.read_bytes()


def backup_path(target: Path) -> Path:
    return target.with_name(target.name + BACKUP_SUFFIX)


def atomic_write(path: Path, data: bytes, metadata: os.stat_result, *, create: bool = False) -> None:
    """Replace from the same filesystem; keep mode and ownership."""
    fd, name = tempfile.mkstemp(prefix=".desktop-swap-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            current = os.fstat(stream.fileno())
            if (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            # link is atomic and refuses any pre-existing backup, including links.
            os.link(name, path)
        else:
            os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inspect(target: Path, version: str) -> dict:
    digest = sha256(read_regular(target))
    state = {ORIGINAL_SHA256: "original", PATCHED_SHA256: "patched"}.get(digest, "unrecognized")
    if version != SUPPORTED_VERSION:
        state = "unsupported-version"
    backup = backup_path(target)
    saved = "absent"
    if backup.exists() or backup.is_symlink():
        saved = "verified" if sha256(read_regular(backup)) == ORIGINAL_SHA256 else "unrecognized"
    return {"version": version, "path": str(target), "state": state, "sha256": digest, "backup": saved}


def manage(action: str, target: Path, version: str, patch_path: Path = DEFAULT_PATCH) -> dict:
    """Public CLI implementation; the path argument is for offline test fixtures."""
    if action not in {"status", "apply", "revert"}:
        raise PatchError("Unknown action.")
    if action == "status":
        return inspect(target, version)
    if version != SUPPORTED_VERSION:
        raise PatchError(f"Only claude-swap {SUPPORTED_VERSION} is supported; found {version}.")
    # Serialize this utility only. Stop cswap first to avoid stale in-memory code;
    # do not run a package upgrade concurrently with apply/revert.
    lock_path = target.with_name(target.name + ".desktop-swap.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = inspect(target, version)
        if before["state"] == "unrecognized" or before["backup"] == "unrecognized":
            raise PatchError("Unrecognized source or backup; nothing was overwritten.")
        if (action, before["state"]) in {("apply", "patched"), ("revert", "original")}:
            return before
        metadata = target.stat()
        original = read_regular(target)
        backup = backup_path(target)
        if action == "apply":
            replacement = patch_bytes(original, patch_path.read_bytes())
            if before["backup"] == "absent":
                atomic_write(backup, original, metadata, create=True)
        else:
            if before["backup"] != "verified":
                raise PatchError("Verified original backup is missing; refusing to reconstruct it.")
            replacement = read_regular(backup)
        if read_regular(target) != original:
            raise PatchError("Source changed during this operation; nothing was overwritten.")
        atomic_write(target, replacement, metadata)
        # Keep the verified original backup for repeatable recovery and inspection.
        return inspect(target, version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "apply", "revert"))
    args = parser.parse_args()
    try:
        distribution = importlib.metadata.distribution("claude-swap")
        target = Path(distribution.locate_file("claude_swap/autoswitch.py"))
        result = manage(args.action, target, distribution.version)
        print(json.dumps(result, indent=2))
        return 0 if result["state"] in {"original", "patched"} else 1
    except importlib.metadata.PackageNotFoundError:
        print("claude-swap is not installed in this interpreter. Run with cswap's own Python.", file=sys.stderr)
    except (PatchError, OSError, UnicodeError) as exc:
        print(f"Quota patch refused: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
