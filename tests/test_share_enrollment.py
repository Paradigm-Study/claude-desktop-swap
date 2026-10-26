"""The Share bearer and ephemeral enrollment proof never enter client state."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from team_pool.client import Client, ClientError, DEVICE_SECRET
from team_pool.share_enrollment import DEFAULT_SHARE_SERVER, ShareTransport, enroll_from_share, saved_token
from test_team_client import FakeNative


POOL = "https://pool.example.test"
TEAM = "team0001"
ACCOUNT = "account001"
BEARER = "ssa_" + "s" * 32
DEVICE = "d" * 43
GRANT = "g" * 43


class Share:
    def __init__(self):
        self.calls = []
        self.response = {"grant": GRANT, "expires_at": 1120, "pool_server": POOL,
                         "team": {"id": TEAM, "name": "Paradigm"}}

    def grant(self, token, challenge, name):
        self.calls.append((token, challenge, name))
        return copy.deepcopy(self.response)


class Pool:
    def __init__(self):
        self.calls = []
        self.response = {"device_id": "device001", "device_token": DEVICE,
                         "share_account_id": ACCOUNT, "share_team_id": TEAM}

    def post(self, path, body, token=None):
        self.calls.append((path, copy.deepcopy(body), token))
        return copy.deepcopy(self.response)


class ShareEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.auth = self.home / "account.json"
        self.auth.write_text(json.dumps({"servers": {DEFAULT_SHARE_SERVER: {
            "token": BEARER, "account": {"id": "untrusted-cache"}, "current": "wrongteam"}}}))
        self.auth.chmod(0o600)
        self.native = FakeNative()
        self.client = Client(self.home / "team.json", native=self.native, preflight=lambda: None)
        self.share, self.pool = Share(), Pool()

    def enroll(self, **kwargs):
        return enroll_from_share(self.client, account_file=self.auth, device_name="Test Mac",
                                 share_transport=self.share, pool_transport=self.pool,
                                 clock=lambda: 1000, **kwargs)

    def test_enroll_keeps_share_bearer_at_share_and_never_writes_native(self):
        result = self.enroll()
        path, body, token = self.pool.calls[0]
        self.assertEqual(path, "/v1/enroll/share")
        self.assertIsNone(token)
        self.assertEqual(self.share.calls[0], (BEARER, hashlib.sha256(body["verifier"].encode()).hexdigest(), "Test Mac"))
        self.assertNotIn(BEARER, json.dumps(self.pool.calls))
        self.assertEqual(self.native.secrets, {DEVICE_SECRET: DEVICE})
        self.assertEqual(self.native.writes, [])
        state = self.client.config.read_text()
        for secret in (BEARER, DEVICE, GRANT, body["verifier"]):
            self.assertNotIn(secret, state)
        self.assertEqual(json.loads(state)["share_account_id"], ACCOUNT)
        self.assertTrue(result["membership_linked"])

    def test_existing_enrollment_refuses_before_requesting_share_grant(self):
        self.enroll()
        self.share.calls.clear()
        with self.assertRaisesRegex(ClientError, "already enrolled"):
            self.enroll()
        self.assertEqual(self.share.calls, [])

    def test_link_preserves_device_and_native_state(self):
        self.enroll()
        before = self.client._load()
        before.update(state="active", managed=True, fingerprint="unchanged")
        self.client._save(before)
        self.pool.response.pop("device_token")
        self.enroll(link=True)
        self.assertEqual(self.pool.calls[-1][0], "/v1/link/share")
        self.assertEqual(self.pool.calls[-1][2], DEVICE)
        self.assertEqual(self.client._load(), before)
        self.assertEqual(self.native.writes, [])

    def test_link_refuses_different_pool_before_sending_device_token(self):
        self.enroll()
        self.pool.calls.clear()
        self.share.response["pool_server"] = "https://other.example.test"
        with self.assertRaisesRegex(ClientError, "another pool"):
            self.enroll(link=True)
        self.assertEqual(self.pool.calls, [])

    def test_expired_grant_and_insecure_pool_are_rejected(self):
        for field, value in (("expires_at", 999), ("expires_at", 2000), ("pool_server", "http://pool.example.test"), ("grant", "bad\nsecret")):
            with self.subTest(field=field, value=value):
                self.share = Share()
                self.share.response[field] = value
                with self.assertRaisesRegex(ClientError, "invalid pool authorization"):
                    self.enroll()
                self.assertEqual(self.pool.calls, [])

    def test_returned_identity_must_match_authorized_team(self):
        self.pool.response["share_team_id"] = "other001"
        with self.assertRaisesRegex(ClientError, "invalid identity"):
            self.enroll()
        self.assertFalse(self.client.config.exists())
        self.assertEqual(self.native.secrets, {})

    def test_saved_token_is_exact_origin_only(self):
        with self.assertRaisesRegex(ClientError, "Sign in"):
            saved_token(self.auth, "https://lookalike.example.test")

    def test_saved_token_requires_private_owned_regular_file(self):
        self.auth.chmod(0o644)
        with self.assertRaises(ClientError):
            saved_token(self.auth, DEFAULT_SHARE_SERVER)
        self.auth.chmod(0o600)
        link = self.home / "link.json"
        link.symlink_to(self.auth)
        with self.assertRaises(ClientError):
            saved_token(link, DEFAULT_SHARE_SERVER)

    def test_transport_sends_token_only_in_header_to_share(self):
        transport = ShareTransport(DEFAULT_SHARE_SERVER)
        captured = []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return b'{}'
        def opening(request, timeout):
            captured.append(request)
            return Response()
        with patch.object(transport.opener, "open", opening):
            transport.grant(BEARER, "a" * 64, "Test Mac")
        request = captured[0]
        self.assertEqual(request.full_url, DEFAULT_SHARE_SERVER + "/api/native-pool/enrollment")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + BEARER)
        self.assertNotIn(BEARER.encode(), request.data)
        self.assertTrue(any(type(handler).__name__ == "_NoRedirect" for handler in transport.opener.handlers))

    def test_module_cli_preserves_safe_error_diagnostic(self):
        env = os.environ.copy()
        result = subprocess.run([sys.executable, "-m", "team_pool.client", "--config", str(self.home / "missing.json"),
                                 "link-share"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        expected = "existing active pool enrollment" if sys.platform == "darwin" else "requires macOS"
        self.assertIn(expected, result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
