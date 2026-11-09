import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

try:
    from team_pool.provision import main, normalize
    from team_pool.server import PoolError
except ImportError:
    raise unittest.SkipTest("Install pilot dependencies")


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.native = {"claudeAiOauth": {
            "accessToken": "synthetic-access", "refreshToken": "synthetic-refresh",
            "expiresAt": 2_000_000_000_000, "scopes": ["user:inference"],
            "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"},
            "mcpOAuth": {"private": "do-not-export"}, "trustedDeviceToken": "do-not-export"}
        self.config = {"oauthAccount": {"accountUuid": "test-account",
                       "emailAddress": "test@example.invalid", "organizationUuid": "test-org"},
                       "projects": {"private": "do-not-export"}}

    def test_exports_only_one_explicit_account_and_real_metadata(self):
        result = normalize(self.native, self.config)
        self.assertEqual(result["expires_at"], 2_000_000_000)
        self.assertEqual(result["refresh_token"], "synthetic-refresh")
        self.assertNotIn("do-not-export", json.dumps(result))
        self.native["claudeAiOauth"].pop("refreshToken")
        with self.assertRaises(PoolError):
            normalize(self.native, self.config)

    def test_file_cli_is_explicit_private_nonoverwriting_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            credentials, config, out = base / "native.json", base / "config.json", base / "prepared.json"
            credentials.write_text(json.dumps(self.native))
            config.write_text(json.dumps(self.config))
            credentials.chmod(0o600)
            config.chmod(0o600)
            args = ["--native-credentials", str(credentials), "--native-config", str(config),
                    "--out", str(out), "--exclusive-owner"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args), 1)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("synthetic-", output.getvalue())
            self.assertEqual(json.loads(out.read_text())["access_token"], "synthetic-access")


if __name__ == "__main__":
    unittest.main()
