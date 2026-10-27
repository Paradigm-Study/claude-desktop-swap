"""Contribution lifecycle without browser interaction or real account material."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from team_pool.client import Client, ClientError, RequestError
from team_pool.contribute import Contribution, authorization_url, hidden_code, run_contribution
from team_pool.provider import CLIENT_ID
from test_team_client import FakeNative, DEVICE_TOKEN


def login_url(**updates):
    query = {"code": "true", "client_id": CLIENT_ID, "response_type": "code",
             "redirect_uri": "https://platform.claude.com/oauth/code/callback",
             "scope": "user:profile user:inference", "code_challenge": "a" * 43,
             "code_challenge_method": "S256", "state": "b" * 43}
    query.update(updates)
    return "https://claude.com/cai/oauth/authorize?" + urlencode(query)


class ContributionTransport:
    def __init__(self):
        self.calls = []
        self.state = "pending"
        self.fail_after_accept = False
        self.result_id = "contribution-a"
        self.url = login_url()

    def post(self, path, body, token=None):
        self.calls.append((path, copy.deepcopy(body), token))
        if path == "/v1/enroll":
            return {"device_id": "device-a", "device_token": DEVICE_TOKEN}
        result = {"contribution_id": self.result_id, "state": self.state, "expires_at": 1600}
        if path.endswith("start"):
            result["authorization_url"] = self.url
        if path.endswith("complete"):
            self.state = result["state"] = "added"
            if self.fail_after_accept:
                raise RequestError()
        if path.endswith("cancel"):
            self.state = result["state"] = "cancelled"
        if self.state == "added":
            result["account_id"] = "shared-account"
        return result


class ContributionClientTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "team.json"
        self.native = FakeNative({"private": "original-credential"})
        self.transport = ContributionTransport()
        self.client = Client(self.path, native=self.native, transport=self.transport,
                             preflight=lambda: None, clock=lambda: 1000)
        self.client.enroll("https://pool.example.invalid", "device-a", "invitation")
        self.flow = Contribution(self.client)
        self.before = copy.deepcopy(self.native.credentials)
        self.code = "synthetic-auth-code#" + "b" * 43

    def test_contribute_keeps_native_store_and_only_persists_receipt(self):
        opened, output = [], []
        def browser(url, **kwargs):
            opened.append(url)
            return True
        result = run_contribution(self.client, open_browser=browser,
                                  read_code=lambda: self.code, output=output.append)
        self.assertEqual(result["state"], "added")
        self.assertEqual(opened, [login_url()])
        self.assertEqual(self.native.credentials, self.before)
        self.assertEqual(self.native.writes, [])
        raw = self.path.read_text()
        for secret in (self.code, DEVICE_TOKEN, "authorization_url", "original-credential"):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, "\n".join(output))
        self.assertEqual(self.client.status()["state"], "enrolled")
        self.assertEqual(self.transport.calls[-1][2], DEVICE_TOKEN)

    def test_lost_response_uses_status_without_replaying_exchange(self):
        result = self.flow.start()
        self.transport.fail_after_accept = True
        with self.assertRaisesRegex(ClientError, "--status"):
            self.flow.complete(result["contribution_id"], self.code)
        self.assertEqual(json.loads(self.path.read_text())["contribution"]["state"], "submitted")
        with self.assertRaises(ClientError):
            self.flow.start()
        with self.assertRaises(ClientError):
            self.flow.complete(result["contribution_id"], self.code)
        self.assertEqual(self.flow.inspect()["state"], "added")
        self.assertEqual(sum(p.endswith("complete") for p, _, _ in self.transport.calls), 1)

    def test_interrupted_prompt_can_resume_and_cancel(self):
        def interrupted():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run_contribution(self.client, no_browser=True, read_code=interrupted, output=lambda _: None)
        self.assertEqual(self.flow.inspect()["state"], "pending")
        self.assertEqual(self.flow.inspect(cancel=True)["state"], "cancelled")
        self.assertEqual(self.native.credentials, self.before)

    def test_interruption_after_submission_preserves_recovery_and_safe_guidance(self):
        output = []
        original = self.transport.post
        def interrupt_after_accept(path, body, token=None):
            result = original(path, body, token)
            if path.endswith("complete"):
                raise KeyboardInterrupt
            return result
        self.transport.post = interrupt_after_accept
        with self.assertRaises(KeyboardInterrupt):
            run_contribution(self.client, no_browser=True, read_code=lambda: self.code, output=output.append)
        self.assertEqual(json.loads(self.path.read_text())["contribution"]["state"], "submitted")
        self.assertIn("--status", output[-1])
        self.assertNotIn("remains pending", output[-1])
        self.assertEqual(self.flow.inspect()["state"], "added")

    def test_browser_failure_prints_validated_fallback_url(self):
        output = []
        def unavailable(*args, **kwargs):
            raise OSError("browser unavailable")
        self.assertEqual(run_contribution(self.client, open_browser=unavailable,
            read_code=lambda: self.code, output=output.append)["state"], "added")
        self.assertTrue(any(login_url() in line for line in output))

    def test_no_browser_option_does_not_open_browser(self):
        with patch("webbrowser.open", side_effect=AssertionError("must not open")):
            run_contribution(self.client, no_browser=True, read_code=lambda: self.code, output=lambda _: None)

    def test_invalid_code_does_not_submit_or_change_pending_state(self):
        operation = self.flow.start()
        for value in ("", "no-state", self.code + "\n", "a#short", "a#" + "b" * 300):
            with self.assertRaises(ClientError):
                self.flow.complete(operation["contribution_id"], value)
        self.assertEqual(self.flow.inspect()["state"], "pending")
        self.assertFalse(any(p.endswith("complete") for p, _, _ in self.transport.calls))

    def test_receipt_mismatch_retains_recovery_marker(self):
        operation = self.flow.start()
        self.transport.result_id = "someone-elses-operation"
        with self.assertRaises(ClientError):
            self.flow.complete(operation["contribution_id"], self.code)
        self.assertEqual(json.loads(self.path.read_text())["contribution"]["state"], "submitted")

    def test_left_or_revoked_device_cannot_contribute(self):
        for value in ("left", "revoked"):
            state = self.client._load()
            state["state"] = value
            self.client._save(state)
            with self.assertRaises(ClientError):
                self.flow.start()
        self.assertEqual(len(self.transport.calls), 1)

    def test_contribution_requires_https_even_for_explicit_local_http(self):
        state = self.client._load()
        state.update(server="http://127.0.0.1:8741", allow_loopback_http=True)
        self.client._save(state)
        with self.assertRaisesRegex(ClientError, "HTTPS"):
            self.flow.start()

    def test_unstarted_status_makes_no_request(self):
        self.assertEqual(self.flow.inspect(), {"state": "none"})
        self.assertEqual(len(self.transport.calls), 1)

    def test_malicious_authorization_url_never_opens_browser(self):
        self.transport.url = "https://attacker.example.invalid/oauth"
        opened = []
        with self.assertRaises(ClientError):
            run_contribution(self.client, open_browser=lambda *a, **kw: opened.append(a),
                             read_code=lambda: self.code, output=lambda _: None)
        self.assertEqual(opened, [])
        self.assertNotIn("contribution", json.loads(self.path.read_text()))

    def test_synced_state_is_not_overwritten_after_waiting_for_browser(self):
        def input_after_sync():
            state = self.client._load()
            state.update(state="active", account_id="other-pool-account", managed=True)
            self.client._save(state)
            return self.code
        run_contribution(self.client, no_browser=True, read_code=input_after_sync, output=lambda _: None)
        self.assertEqual(self.client._load()["account_id"], "other-pool-account")
        self.assertEqual(self.client._load()["state"], "active")


class ContributionInputTests(unittest.TestCase):
    def test_only_expected_official_auth_urls_pass(self):
        self.assertEqual(authorization_url(login_url()), login_url())
        bad = [login_url().replace("claude.com", "claude.com.evil.invalid", 1),
               login_url().replace("https://", "http://", 1),
               login_url() + "&state=duplicate", login_url() + "#fragment",
               login_url(redirect_uri="https://evil.invalid/callback"),
               login_url(client_id="other-client"), login_url(code_challenge_method="plain"),
               login_url(scope="user:profile user:inference unknown:scope")]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ClientError):
                authorization_url(url)

    def test_hidden_input_refuses_non_tty_and_echo_fallback(self):
        with patch("sys.stdin", io.StringIO("secret")), self.assertRaises(ClientError):
            hidden_code()
        with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass") as prompt:
            import getpass
            prompt.side_effect = getpass.GetPassWarning("echo unavailable")
            with self.assertRaises(ClientError):
                hidden_code()


if __name__ == "__main__":
    unittest.main()
