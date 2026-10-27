"""Member menu actions use the same guarded native writer and contribution journal."""
import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from team_pool.catalog import validate_catalog
from team_pool.client import Client, ClientError, DEVICE_SECRET, RequestError, _lock
from team_pool.menu_bridge import dispatch, main
from test_team_client import FakeNative, FakeTransport, assignment, DEVICE_TOKEN, PROVIDER_TOKEN
from test_team_contribute import ContributionTransport


def catalog():
    return {"account_id": "account-a", "threshold": 98, "accounts": [
        {"id": "account-a", "account": {"emailAddress": "a@example.invalid", "subscriptionType": "max"},
         "usage": {"five_hour": 10, "seven_day": 86}, "fetched": 999, "expires_at": 4000,
         "state": "ready", "eligible": True, "reason": None},
        {"id": "account-b", "account": {"emailAddress": "b@example.invalid", "subscriptionType": "max"},
         "usage": {"five_hour": 0, "seven_day": 100}, "fetched": 999, "expires_at": 4000,
         "state": "ready", "eligible": False, "reason": "quota-exhausted"},
    ]}


class PoolMenuTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.native = FakeNative({"personal": "original-credentials"})
        self.transport = FakeTransport()
        self.client = Client(Path(self.temporary.name) / "team.json", native=self.native,
                             transport=self.transport, preflight=lambda: None, clock=lambda: 1000)
        self.client.enroll("https://pool.example.invalid", "fixture Mac", "fixture invite")
        self.client.sync(take_over_native_store=True)

    def run_bridge(self, request, **kwargs):
        output = io.StringIO()
        with patch("team_pool.menu_bridge.sys.platform", "darwin"):
            code = main(input_stream=io.StringIO(json.dumps(request)), output_stream=output,
                        factory=lambda: self.client, **kwargs)
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_catalog_reads_member_metadata_without_native_writes_or_state_changes(self):
        self.transport.next = catalog()
        before, writes = self.client.config.read_bytes(), len(self.native.writes)
        result = dispatch({"action": "list"}, self.client)
        self.assertEqual(len(result["accounts"]), 2)
        self.assertEqual(result["account_id"], "account-a")
        self.assertEqual(result["client_state"], "active")
        self.assertEqual(len(self.native.writes), writes)
        self.assertEqual(before, self.client.config.read_bytes())
        self.assertEqual(self.transport.calls[-1], ("/v1/accounts/list", {}, DEVICE_TOKEN))
        self.assertNotIn(DEVICE_TOKEN, json.dumps(result))
        self.assertNotIn(PROVIDER_TOKEN, json.dumps(result))

    def test_catalog_current_marker_uses_local_commit_not_server_decision(self):
        self.transport.next = catalog()
        self.transport.next["account_id"] = "account-b"
        self.assertEqual(dispatch({"action": "list"}, self.client)["account_id"], "account-a")

    def test_catalog_accepts_provider_legacy_and_named_model_quotas(self):
        value = catalog()
        value["accounts"][0]["usage"].update(seven_day_sonnet=25, **{"weekly_scoped:claude sonnet 4.5": 30})
        self.assertEqual(validate_catalog(value)["accounts"][0]["usage"], value["accounts"][0]["usage"])

    def test_catalog_rejects_credential_fields_duplicates_and_invalid_usage(self):
        bad = []
        value = catalog(); value["accounts"][0]["access_token"] = PROVIDER_TOKEN; bad.append(value)
        value = catalog(); value["accounts"][0]["account"]["refresh_token"] = "secret"; bad.append(value)
        value = catalog(); value["accounts"][1]["id"] = "account-a"; bad.append(value)
        value = catalog(); value["accounts"][0]["usage"]["five_hour"] = float("nan"); bad.append(value)
        value = catalog(); value["accounts"][1]["eligible"] = True; bad.append(value)
        value = catalog(); value["accounts"][0]["account"]["emailAddress"] = "bad\nlabel"; bad.append(value)
        value = catalog(); value["accounts"][0]["usage"]["private_credential"] = 0; bad.append(value)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ClientError):
                validate_catalog(value)

    def test_listing_revocation_marks_existing_device_and_returns_no_catalog(self):
        self.transport.error = RequestError(401)
        code, result, raw = self.run_bridge({"action": "list"})
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(self.client._load()["state"], "revoked")
        self.assertNotIn("accounts", result)
        self.assertNotIn(DEVICE_TOKEN, raw)

    def test_listing_deployment_delay_does_not_pause_or_overwrite_native(self):
        self.transport.error = RequestError(404)
        before = self.client.config.read_bytes()
        code, result, raw = self.run_bridge({"action": "list"})
        self.assertEqual(code, 1)
        self.assertIn("still updating", result["error"])
        self.assertEqual(before, self.client.config.read_bytes())

    def test_manual_switch_uses_guarded_commit_and_preserves_backup(self):
        before = copy.deepcopy(self.native.original)
        self.transport.next = assignment(account_id="account-c", access_token="fixture-c")
        allowed = []
        original = self.native.apply
        def apply(*args, **kwargs):
            allowed.append(self.native.interaction_allowed)
            return original(*args, **kwargs)
        self.native.apply = apply
        result = dispatch({"action": "switch", "account_id": "account-c"}, self.client, running=lambda: True)
        self.assertEqual(result["account_id"], "account-c")
        self.assertEqual(self.native.credentials["account_id"], "account-c")
        self.assertEqual(self.native.original, before)
        self.assertEqual(self.transport.calls[-1], ("/v1/assignment", {"account_id": "account-c"}, DEVICE_TOKEN))
        self.assertEqual(allowed, [False])
        self.assertNotIn("fixture-c", json.dumps(result))

    def test_unavailable_selection_preserves_local_assignment_and_active_state(self):
        before, writes = self.client.config.read_bytes(), len(self.native.writes)
        self.transport.error = RequestError(409)
        with self.assertRaisesRegex(ClientError, "unavailable"):
            dispatch({"action": "switch", "account_id": "account-b"}, self.client, running=lambda: True)
        self.assertEqual(before, self.client.config.read_bytes())
        self.assertEqual(writes, len(self.native.writes))

    def test_wrong_selection_response_never_writes_the_unrequested_account(self):
        writes = len(self.native.writes)
        with self.assertRaisesRegex(ClientError, "did not confirm"):
            dispatch({"action": "switch", "account_id": "account-b"}, self.client, running=lambda: True)
        self.assertEqual(writes, len(self.native.writes))

    def test_manual_native_login_and_paused_state_block_selection_before_request(self):
        calls = len(self.transport.calls)
        self.native.credentials = {"manual": "login"}
        with self.assertRaisesRegex(ClientError, "Native credentials changed"):
            dispatch({"action": "switch", "account_id": "account-b"}, self.client, running=lambda: True)
        self.assertEqual(len(self.transport.calls), calls)
        with self.assertRaisesRegex(ClientError, "paused"):
            dispatch({"action": "switch", "account_id": "account-b"}, self.client, running=lambda: True)
        self.assertEqual(len(self.transport.calls), calls)

    def test_stopped_background_service_blocks_manual_switch(self):
        calls = len(self.transport.calls)
        with self.assertRaisesRegex(ClientError, "stopped"):
            dispatch({"action": "switch", "account_id": "account-b"}, self.client, running=lambda: False)
        self.assertEqual(len(self.transport.calls), calls)

    def test_null_or_invalid_manual_selection_never_becomes_automatic_sync(self):
        calls = len(self.transport.calls)
        for value in (None, "", "bad/id", [], False):
            with self.subTest(value=value), self.assertRaises(ClientError):
                dispatch({"action": "switch", "account_id": value}, self.client, running=lambda: True)
        self.assertEqual(len(self.transport.calls), calls)

    def test_busy_background_lock_returns_promptly_before_request_or_write(self):
        self.client.lock_timeout = 0.02
        calls, writes = len(self.transport.calls), len(self.native.writes)
        started = time.monotonic()
        with _lock(self.client.config):
            with self.assertRaisesRegex(ClientError, "in progress"):
                dispatch({"action": "switch", "account_id": "account-b"}, self.client, running=lambda: True)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(len(self.native.writes), writes)

    def test_menu_contribution_uses_private_input_and_persists_only_receipts(self):
        self.client.transport = ContributionTransport()
        before, backup = copy.deepcopy(self.native.credentials), copy.deepcopy(self.native.original)
        code, start, raw = self.run_bridge({"action": "contribute-start"})
        self.assertEqual(code, 0)
        self.assertIn("authorization_url", start["result"])
        authorization_code = "fixture-oauth-code#" + "b" * 43
        code, complete, raw = self.run_bridge({"action": "contribute-complete",
            "contribution_id": start["result"]["contribution_id"], "code": authorization_code})
        self.assertEqual(complete["result"]["state"], "added")
        self.assertEqual(self.native.credentials, before)
        self.assertEqual(self.native.original, backup)
        self.assertNotIn(authorization_code, raw)
        self.assertNotIn(authorization_code, self.client.config.read_text())

    def test_uncertain_contribution_uses_status_and_does_not_resubmit(self):
        self.client.transport = transport = ContributionTransport()
        start = dispatch({"action": "contribute-start"}, self.client)
        transport.fail_after_accept = True
        request = {"action": "contribute-complete", "contribution_id": start["contribution_id"],
                   "code": "fixture-code#" + "b" * 43}
        with self.assertRaises(ClientError):
            dispatch(request, self.client)
        with self.assertRaises(ClientError):
            dispatch(request, self.client)
        self.assertEqual(dispatch({"action": "contribute-status"}, self.client)["state"], "added")
        self.assertEqual(sum(path.endswith("complete") for path, _, _ in transport.calls), 1)

    def test_unexpected_errors_never_echo_request_or_backend_secret(self):
        self.client.list_accounts = lambda: (_ for _ in ()).throw(RuntimeError("private-provider-token"))
        code, result, raw = self.run_bridge({"action": "list"})
        self.assertEqual(code, 1)
        self.assertNotIn("private-provider-token", raw)
        self.assertFalse(result["ok"])

    def test_unknown_fields_rejected_without_actions(self):
        count = len(self.transport.calls)
        code, result, raw = self.run_bridge({"action": "list", "admin_token": "private-secret"})
        self.assertEqual(code, 1)
        self.assertNotIn("private-secret", raw)
        self.assertEqual(count, len(self.transport.calls))


if __name__ == "__main__":
    unittest.main()
