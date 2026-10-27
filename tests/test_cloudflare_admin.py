import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("cloudflare_admin", Path(__file__).resolve().parents[1] / "scripts/cloudflare_pool_admin.py")
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


class AdminTests(unittest.TestCase):
    def test_private_configuration_rejects_readable_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text('{}')
            path.chmod(0o644)
            with self.assertRaises(admin.AdminError):
                admin.private_json(path)
            path.chmod(0o600)
            self.assertEqual(admin.private_json(path), {})
            link = Path(folder) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                admin.private_json(link)

    def test_invite_saved_privately_and_never_returned(self):
        client = admin.Admin({"server": "https://pool.example.com", "admin_token": "a" * 43})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.invite"
            with patch.object(client, 'post', return_value={"invitation": "b" * 43, "expires_at": 123}) as post:
                result = client.invite(path, 86400)
                self.assertNotIn("b" * 43, json.dumps(result))
                self.assertEqual(path.read_text(), "b" * 43 + "\n")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    client.invite(path, 86400)
                self.assertEqual(post.call_count, 1)

    def test_failed_invite_does_not_leave_a_false_usable_file(self):
        client = admin.Admin({"server": "https://pool.example.com", "admin_token": "a" * 43})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.invite"
            with patch.object(client, 'post', side_effect=admin.AdminError("failed")):
                with self.assertRaises(admin.AdminError):
                    client.invite(path, 86400)
            self.assertFalse(path.exists())

    def test_credential_origin_restrictions(self):
        for url in ("http://pool.example.com", "https://user@pool.example.com",
                    "https://pool.example.com/other", "https://pool.example.com?", "https://pool.example.com#"):
            with self.subTest(url=url), self.assertRaises(admin.AdminError):
                admin.Admin({"server": url, "admin_token": "a" * 43})

    def test_bearer_redirects_are_not_followed(self):
        self.assertIsNone(admin.NoRedirect().redirect_request(None, None, 302, None,
            {}, "https://other.example.com"))

    def test_transport_diagnostics_do_not_include_remote_errors(self):
        client = admin.Admin({"server": "https://pool.example.com", "admin_token": "secret" * 8})
        with patch.object(client.opener, 'open', side_effect=ValueError("provider-secret")):
            with self.assertRaises(admin.AdminError) as result:
                client.post('/admin/accounts/list', {})
        self.assertNotIn('provider-secret', str(result.exception))
        self.assertNotIn('secret' * 8, str(result.exception))


if __name__ == '__main__':
    unittest.main()
