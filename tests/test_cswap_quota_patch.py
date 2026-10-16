"""Utility guards use synthetic source files, never the installed cswap."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cswap_quota_patch as quota

ORIGINAL = b"first\nold\nlast\n"
PATCHED = b"first\nnew\nlast\n"
DIFF = b"--- a/claude_swap/autoswitch.py\n+++ b/claude_swap/autoswitch.py\n@@ -1,3 +1,3 @@\n first\n-old\n+new\n last\n"


class QuotaPatchTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.target, self.diff = self.root / "autoswitch.py", self.root / "fix.patch"
        self.target.write_bytes(ORIGINAL)
        self.target.chmod(0o640)
        self.diff.write_bytes(DIFF)
        hashes = patch.multiple(quota, ORIGINAL_SHA256=quota.sha256(ORIGINAL),
                                PATCHED_SHA256=quota.sha256(PATCHED))
        hashes.start()
        self.addCleanup(hashes.stop)

    def manage(self, action, version="0.26.0"):
        return quota.manage(action, self.target, version, self.diff)

    def test_status_has_no_side_effects(self):
        self.assertEqual(self.manage("status")["state"], "original")
        self.assertEqual(set(self.root.iterdir()), {self.target, self.diff})

    def test_apply_revert_idempotence_and_permissions(self):
        before = self.target.stat()
        for _ in range(2):
            self.assertEqual(self.manage("apply")["state"], "patched")
            self.assertEqual(self.target.read_bytes(), PATCHED)
            self.assertEqual(quota.backup_path(self.target).read_bytes(), ORIGINAL)
        for _ in range(2):
            self.assertEqual(self.manage("revert")["state"], "original")
            self.assertEqual(self.target.read_bytes(), ORIGINAL)
        after = self.target.stat()
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o640)
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(self.manage("apply")["state"], "patched")

    def test_version_guard_does_not_create_a_lock_or_backup(self):
        self.assertEqual(self.manage("status", "0.27.0")["state"], "unsupported-version")
        with self.assertRaises(quota.PatchError):
            self.manage("apply", "0.27.0")
        self.assertEqual(self.target.read_bytes(), ORIGINAL)
        self.assertEqual(set(self.root.iterdir()), {self.target, self.diff})

    def test_unknown_source_cannot_be_overwritten(self):
        self.target.write_bytes(b"local changes\n")
        for action in ("apply", "revert"):
            with self.assertRaises(quota.PatchError):
                self.manage(action)
        self.assertEqual(self.target.read_bytes(), b"local changes\n")
        self.assertFalse(quota.backup_path(self.target).exists())

    def test_wrong_backup_blocks_apply_and_revert(self):
        quota.backup_path(self.target).write_bytes(b"other data")
        for state in (ORIGINAL, PATCHED):
            self.target.write_bytes(state)
            for action in ("apply", "revert"):
                with self.assertRaises(quota.PatchError):
                    self.manage(action)
                self.assertEqual(self.target.read_bytes(), state)

    def test_missing_backup_blocks_revert(self):
        self.target.write_bytes(PATCHED)
        self.assertEqual(self.manage("status")["state"], "patched")
        with self.assertRaises(quota.PatchError):
            self.manage("revert")
        self.assertEqual(self.target.read_bytes(), PATCHED)

    def test_malformed_patch_never_creates_backup(self):
        for bad in (DIFF.replace(b" first", b" nope"),
                    DIFF.replace(b"-1,3", b"-1,4"),
                    DIFF.replace(b"+new", b"+unexpected"),
                    DIFF.replace(b"a/claude_swap", b"a/other")):
            self.diff.write_bytes(bad)
            with self.assertRaises(quota.PatchError):
                self.manage("apply")
            self.assertEqual(self.target.read_bytes(), ORIGINAL)
            self.assertFalse(quota.backup_path(self.target).exists())

    def test_symlink_source_and_backup_are_rejected(self):
        other = self.root / "other.py"
        other.write_bytes(ORIGINAL)
        self.target.unlink()
        self.target.symlink_to(other)
        with self.assertRaises(quota.PatchError):
            self.manage("apply")
        self.target.unlink()
        self.target.write_bytes(ORIGINAL)
        quota.backup_path(self.target).symlink_to(other)
        with self.assertRaises(quota.PatchError):
            self.manage("apply")
        self.assertEqual(other.read_bytes(), ORIGINAL)

    def test_failed_atomic_replace_leaves_source_and_verified_backup(self):
        with patch.object(quota.os, "replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                self.manage("apply")
        self.assertEqual(self.target.read_bytes(), ORIGINAL)
        self.assertEqual(quota.backup_path(self.target).read_bytes(), ORIGINAL)
        self.assertEqual(list(self.root.glob(".desktop-swap-*")), [])
        self.assertEqual(self.manage("apply")["state"], "patched")

    def test_concurrent_source_change_is_not_overwritten(self):
        original_write = quota.atomic_write
        def change_after_backup(*args, **kwargs):
            original_write(*args, **kwargs)
            self.target.write_bytes(b"concurrent change\n")
        with patch.object(quota, "atomic_write", side_effect=change_after_backup):
            with self.assertRaises(quota.PatchError):
                self.manage("apply")
        self.assertEqual(self.target.read_bytes(), b"concurrent change\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
