"""Two simulated Macs over real TLS; no real credentials or Anthropic calls."""
import datetime
import ipaddress
import json
import os
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    raise unittest.SkipTest("Install the pilot dependencies for TLS integration tests")

from team_pool.client import Client, PausedError, RequestError
from team_pool.native import NativeStore, NATIVE_SERVICE
from team_pool.server import Coordinator, PoolHTTPServer
from team_pool.contribute import Contribution
from team_pool.provider import Provider


class MemoryKeychain:
    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, value):
        self.items[service, username] = value

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


class Clock:
    now = 2_000_000_000

    def __call__(self):
        return self.now


class FakeAnthropic:
    """Provider boundary with real rotation semantics and observable grant count."""
    def __init__(self, clock):
        self.clock = clock
        self.usage_pct = {"a": 10, "b": 20}
        self.grants = {"a": 0, "b": 0}
        self.credentials = {}
        self.token_accounts = {}
        self.exchanges = []
        for account in ("a", "b"):
            item = {
                "access_token": "synthetic-access-" + account,
                "refresh_token": "synthetic-refresh-" + account,
                "expires_at": clock() + 3600,
                "scopes": ["user:inference", "user:profile"],
                "account": {
                    "accountUuid": "account-" + account,
                    "organizationUuid": "org-" + account,
                    "emailAddress": account + "@example.invalid",
                    "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x",
                },
            }
            self.credentials[account] = item
            self.token_accounts[item["access_token"]] = account

    def authorization_url(self, verifier, state):
        return Provider().authorization_url(verifier, state)

    def exchange_code(self, code, verifier, state):
        self.exchanges.append((code, verifier, state))
        return dict(self.credentials[code[-1]])

    def identity(self, token):
        return self.credentials[self.token_accounts[token]]["account"]

    def usage(self, token):
        return {"five_hour": self.usage_pct[self.token_accounts[token]], "seven_day": 10}

    def refresh(self, credentials):
        key = credentials["account"]["accountUuid"][-1]
        self.grants[key] += 1
        successor = dict(credentials,
                         access_token=f"synthetic-access-{key}-{self.grants[key]}",
                         refresh_token=f"synthetic-refresh-{key}-{self.grants[key]}",
                         expires_at=self.clock() + 3600)
        self.credentials[key] = successor
        self.token_accounts[successor["access_token"]] = key
        return successor


class TwoMacPilotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.clock = Clock()
        self.provider = FakeAnthropic(self.clock)
        self.pool = Coordinator(self.root / "pool", provider=self.provider, clock=self.clock, create=True)
        self.addCleanup(self.pool.close)
        for key, credentials in self.provider.credentials.items():
            self.pool.add_account(key, credentials, exclusive_owner=True)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pilot-test.invalid")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        self.cert_path = self.root / "test-cert.pem"
        key_path = self.root / "test-key.pem"
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_path, key_path)
        self.server = PoolHTTPServer(("127.0.0.1", 0), self.pool)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"https://127.0.0.1:{self.server.server_port}"
        self.trust = patch.dict(os.environ, {"SSL_CERT_FILE": str(self.cert_path),
                                           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        self.trust.start()
        self.addCleanup(self.trust.stop)
        self.clients = [self.new_mac("one"), self.new_mac("two")]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def new_mac(self, name):
        home = self.root / name
        home.mkdir(mode=0o700)
        (home / ".claude").mkdir(mode=0o700)
        (home / ".claude.json").write_text(json.dumps({"projects": {"local": name}}))
        backend = MemoryKeychain()
        backend.set_password(NATIVE_SERVICE, name, json.dumps({
            "claudeAiOauth": {"accessToken": "original-" + name, "refreshToken": "original-refresh-" + name},
            "mcpOAuth": {"private": "local-mcp-" + name},
        }))
        native = NativeStore(home, backend, policy_paths=[], environ={"USER": name}, clock=self.clock)
        client = Client(home / "client.json", native=native, clock=self.clock, preflight=lambda: None)
        client.enroll(self.url, name, self.pool.invite())
        return client

    def test_two_macs_enroll_rotate_refresh_pause_and_revoke(self):
        one, two = self.clients
        first = one.sync(take_over_native_store=True)
        second = two.sync(take_over_native_store=True)
        self.assertEqual((first["account_id"], second["account_id"]), ("a", "b"))
        for client in self.clients:
            name = client.native.username
            credential = json.loads(client.native.backend.get_password(NATIVE_SERVICE, name))
            self.assertNotIn("refreshToken", credential["claudeAiOauth"])
            self.assertEqual(credential["mcpOAuth"]["private"], "local-mcp-" + name)
            self.assertEqual(json.loads(client.native.config.read_text())["projects"], {"local": name})
            state = client.config.read_text()
            self.assertNotIn("synthetic-access", state)
            self.assertNotIn(client.native.get_secret("device-token"), state)

        self.provider.usage_pct["a"] = 95
        self.clock.now += 121
        self.assertEqual(one.sync()["account_id"], "b")
        self.clock.now += 3200
        one.sync()
        two.sync()
        self.assertEqual(self.provider.grants, {"a": 1, "b": 1})
        self.assertEqual(json.loads(one.native.backend.get_password(NATIVE_SERVICE, "one"))
                         ["claudeAiOauth"]["accessToken"], "synthetic-access-b-1")

        # Manual account change wins over the coordinator on one device.
        edited = {"claudeAiOauth": {"accessToken": "manual-login"}}
        one.native.backend.set_password(NATIVE_SERVICE, "one", json.dumps(edited))
        with self.assertRaises(PausedError):
            one.sync()
        self.assertEqual(one.status()["state"], "paused")
        self.assertEqual(json.loads(one.native.backend.get_password(NATIVE_SERVICE, "one")), edited)

        # Revocation stops delivery; it does not magically revoke the provider bearer.
        self.pool.revoke(two.status()["device_id"])
        before = two.native.backend.get_password(NATIVE_SERVICE, "two")
        with self.assertRaises(RequestError):
            two.sync()
        self.assertEqual(two.status()["state"], "revoked")
        self.assertEqual(two.native.backend.get_password(NATIVE_SERVICE, "two"), before)
        two.leave(restore=True)
        restored = json.loads(two.native.backend.get_password(NATIVE_SERVICE, "two"))
        self.assertEqual(restored["claudeAiOauth"]["accessToken"], "original-two")

    def test_https_requires_trusted_certificate(self):
        from team_pool.client import Transport
        with patch.dict(os.environ, {"SSL_CERT_FILE": str(self.root / "missing-ca.pem"),
                                    "SSL_CERT_DIR": str(self.root / "no-ca-dir")}):
            with self.assertRaises(RequestError):
                Transport(self.url).post("/v1/enroll", {
                    "invitation": self.pool.invite(), "device_name": "untrusted"})

    def test_teammates_contribute_over_https_then_share_and_renew_native_pool(self):
        # Start with no admin-imported accounts: teammates populate the pool.
        with self.pool.lock, self.pool.db:
            self.pool.db.execute("DELETE FROM accounts")
        one, two = self.clients
        originals = [client.native.snapshot() for client in self.clients]
        flow_one, flow_two = Contribution(one), Contribution(two)
        first = flow_one.start()
        state = parse_qs(urlsplit(first["authorization_url"]).query)["state"][0]
        added = flow_one.complete(first["contribution_id"], "synthetic-code-a#" + state)
        self.assertEqual(added["state"], "added")
        self.assertEqual(flow_one.inspect(), added)
        self.assertEqual([client.native.snapshot() for client in self.clients], originals)
        self.assertEqual(len(self.provider.exchanges), 1)

        # A different Mac can use the contribution without receiving its refresh token.
        for client in self.clients:
            self.assertEqual(client.sync(take_over_native_store=True)["account_id"], added["account_id"])
        before = [client.native.snapshot() for client in self.clients]
        second = flow_two.start()
        state = parse_qs(urlsplit(second["authorization_url"]).query)["state"][0]
        new = flow_two.complete(second["contribution_id"], "synthetic-code-b#" + state)
        self.assertEqual(new["state"], "added")
        self.assertEqual([client.native.snapshot() for client in self.clients], before)
        self.assertEqual(len(self.pool.accounts()), 2)

        # Both switch to the fresh account when the first reaches the threshold.
        self.provider.usage_pct["a"] = 95
        self.clock.now += 121
        for client in self.clients:
            self.assertEqual(client.sync()["account_id"], new["account_id"])
        self.clock.now += 3200
        for client in self.clients:
            client.sync()
            name = client.native.username
            native = json.loads(client.native.backend.get_password(NATIVE_SERVICE, name))
            self.assertNotIn("refreshToken", native["claudeAiOauth"])
            self.assertEqual(native["claudeAiOauth"]["accessToken"], "synthetic-access-b-1")
            self.assertEqual(native["mcpOAuth"]["private"], "local-mcp-" + name)
            self.assertEqual(json.loads(client.native.config.read_text())["projects"], {"local": name})
            self.assertNotIn("synthetic-code", client.config.read_text())
        self.assertEqual(self.provider.grants, {"a": 1, "b": 1})

    def test_contribution_lost_response_recovers_receipt_over_https(self):
        one = self.clients[0]
        flow = Contribution(one)
        operation = flow.start()
        state = parse_qs(urlsplit(operation["authorization_url"]).query)["state"][0]
        # Inject a lost response AFTER the real HTTPS endpoint commits its receipt.
        real = one._transport(one._load())
        class LostResponse:
            def post(self, path, body, token=None):
                result = real.post(path, body, token)
                if path.endswith("complete"):
                    raise RequestError()
                return result
        one.transport = LostResponse()
        from team_pool.client import ClientError
        with self.assertRaises(ClientError):
            flow.complete(operation["contribution_id"], "synthetic-code-a#" + state)
        receipt = flow.inspect()
        self.assertEqual(receipt["state"], "duplicate")
        self.assertEqual(receipt["account_id"], "a")
        self.assertEqual(len(self.provider.exchanges), 1)
        self.assertEqual(len(self.pool.accounts()), 2)


if __name__ == "__main__":
    unittest.main()
