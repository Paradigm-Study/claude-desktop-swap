"""Offline contribution protocol and crash-recovery tests; no native store writes."""
import concurrent.futures
import copy
import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from team_pool.server import Coordinator, PoolError, PoolHTTPServer


def credentials(name='donated'):
    return {'access_token': 'access-secret-' + name, 'refresh_token': 'refresh-secret-' + name,
            'expires_at': 10000, 'scopes': ['user:inference', 'user:profile'],
            'account': {'accountUuid': name, 'emailAddress': name + '@example.test',
                        'organizationUuid': 'org-' + name, 'subscriptionType': 'max',
                        'rateLimitTier': 'default_claude_max_20x'}}


class OAuthProvider:
    def __init__(self):
        self.calls = []
        self.grant = credentials()
        self.failure = None

    def authorization_url(self, verifier, state):
        return 'https://claude.ai/oauth/authorize?' + urllib.parse.urlencode({'state': state, 'challenge': 'public-challenge'})

    def exchange_code(self, code, verifier, state):
        self.calls.append((code, verifier, state))
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.grant)

    def usage(self, access):
        return {'five_hour': 0, 'seven_day': 0}

    def identity(self, access):
        return self.grant['account']


class ContributionServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state'
        self.now = 1000
        self.provider = OAuthProvider()
        self.pool = Coordinator(self.path, provider=self.provider, clock=lambda: self.now, create=True)
        self.first = self.enroll('First Mac')
        self.second = self.enroll('Second Mac')

    def tearDown(self):
        self.pool.close()
        self.temp.cleanup()

    def enroll(self, name):
        return self.pool.enroll(self.pool.invite(), name)

    def start(self, device=None):
        return self.pool.contribution_start((device or self.first)['device_token'])

    def code(self, operation, code='manual-auth-code'):
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(operation['authorization_url']).query)['state'][0]
        return code + '#' + state

    def finish(self, operation, device=None):
        return self.pool.contribution_complete((device or self.first)['device_token'], operation['contribution_id'], self.code(operation))

    def restart(self):
        self.pool.close()
        self.pool = Coordinator(self.path, provider=self.provider, clock=lambda: self.now)

    def test_add_is_atomic_idempotent_and_shared_with_both_devices(self):
        operation = self.start()
        self.assertEqual(operation['state'], 'pending')
        self.assertEqual(operation['expires_at'], 1600)
        self.assertEqual(self.provider.calls, [])
        receipt = self.finish(operation)
        self.assertEqual(receipt['state'], 'added')
        self.assertEqual(set(receipt), {'contribution_id', 'expires_at', 'state', 'account_id'})
        self.assertEqual(self.finish(operation), receipt)
        self.assertEqual(len(self.provider.calls), 1)
        for device in (self.first, self.second):
            assignment = self.pool.assignment(device['device_token'])
            self.assertEqual(assignment['account_id'], receipt['account_id'])
            self.assertEqual(assignment['account']['accountUuid'], 'donated')
            self.assertNotIn('refresh_token', assignment)
        self.restart()
        self.assertEqual(self.finish(operation), receipt)
        self.assertEqual(len(self.provider.calls), 1)

    def test_one_pending_per_device_and_distinct_device_requests(self):
        operation = self.start()
        self.assertEqual(self.start(), operation)
        self.assertNotEqual(self.start(self.second)['contribution_id'], operation['contribution_id'])
        self.assertEqual(self.pool.db.execute("SELECT count(*) FROM contributions WHERE device_id=? AND state='pending'", (self.first['device_id'],)).fetchone()[0], 1)

    def test_cross_device_unknown_and_revoked_are_rejected_before_exchange(self):
        operation = self.start()
        for method in (self.pool.contribution_status, self.pool.contribution_cancel):
            with self.assertRaises(PoolError) as caught:
                method(self.second['device_token'], operation['contribution_id'])
            self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(PoolError) as caught:
            self.finish(operation, self.second)
        self.assertEqual(caught.exception.status, 404)
        self.pool.revoke(self.first['device_id'])
        for invoke in (lambda: self.start(), lambda: self.finish(operation),
                       lambda: self.pool.contribution_status(self.first['device_token'], operation['contribution_id']),
                       lambda: self.pool.contribution_cancel(self.first['device_token'], operation['contribution_id'])):
            with self.assertRaises(PoolError) as caught:
                invoke()
            self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.provider.calls, [])

    def test_wrong_callback_state_cannot_consume_grant_and_attempts_are_bounded(self):
        operation = self.start()
        wrong_code = 'manual-auth-code#' + 'x' * 43
        for _ in range(2):
            with self.assertRaises(PoolError) as caught:
                self.pool.contribution_complete(self.first['device_token'], operation['contribution_id'], wrong_code)
            self.assertEqual(caught.exception.code, 'invalid-authorization-code')
        receipt = self.pool.contribution_complete(self.first['device_token'], operation['contribution_id'], wrong_code)
        self.assertEqual(receipt['state'], 'failed')
        self.assertEqual(self.finish(operation), receipt)
        self.assertIsNone(self.pool.db.execute('SELECT secret FROM contributions').fetchone()[0])
        self.assertEqual(self.provider.calls, [])

    def test_malformed_codes_reject_locally(self):
        for bad in (None, 42, 'code-no-state', 'code#short', 'a#b#c', 'x' * 3000, 'white space#' + 'x' * 43):
            operation = self.start()
            with self.assertRaises(PoolError):
                self.pool.contribution_complete(self.first['device_token'], operation['contribution_id'], bad)
            self.pool.contribution_cancel(self.first['device_token'], operation['contribution_id'])
            self.now += 3601
        self.assertEqual(self.provider.calls, [])

    def test_expired_and_cancelled_operations_cannot_exchange(self):
        operation = self.start()
        self.now = operation['expires_at']
        self.assertEqual(self.finish(operation)['state'], 'expired')
        replacement = self.start()
        self.assertNotEqual(operation['contribution_id'], replacement['contribution_id'])
        cancelled = self.pool.contribution_cancel(self.first['device_token'], replacement['contribution_id'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(self.finish(replacement), cancelled)
        self.assertEqual(self.pool.contribution_cancel(self.first['device_token'], replacement['contribution_id']), cancelled)
        self.assertEqual(self.provider.calls, [])
        self.assertTrue(all(row[0] is None for row in self.pool.db.execute('SELECT secret FROM contributions')))

    def test_pending_expires_across_restart(self):
        operation = self.start()
        self.now += 601
        self.restart()
        self.assertEqual(self.finish(operation)['state'], 'expired')
        self.assertEqual(self.provider.calls, [])

    def test_start_limits_persist_across_restart(self):
        for _ in range(5):
            operation = self.start()
            self.pool.contribution_cancel(self.first['device_token'], operation['contribution_id'])
        self.restart()
        with self.assertRaises(PoolError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 429)
        # Another enrolled device has an independent limit.
        self.start(self.second)
        for batch in range(3):
            self.now += 3601
            for _ in range(5):
                operation = self.start()
                self.pool.contribution_cancel(self.first['device_token'], operation['contribution_id'])
        self.now += 3601
        with self.assertRaises(PoolError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 429)

    def test_duplicate_provider_account_never_replaces_existing_lineage(self):
        self.pool.add_account('existing', credentials(), exclusive_owner=True)
        before = self.pool.db.execute("SELECT secret,generation FROM accounts WHERE id='existing'").fetchone()
        self.provider.grant['refresh_token'] = 'new-unrelated-grant-lineage'
        operation = self.start()
        receipt = self.finish(operation)
        self.assertEqual(receipt['state'], 'duplicate')
        self.assertEqual(receipt['account_id'], 'existing')
        after = self.pool.db.execute("SELECT secret,generation FROM accounts WHERE id='existing'").fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(len(self.pool.accounts()), 1)
        self.assertEqual(self.finish(operation), receipt)
        self.assertEqual(len(self.provider.calls), 1)

    def test_concurrent_completions_exchange_once(self):
        operation = self.start()
        with concurrent.futures.ThreadPoolExecutor(8) as executor:
            receipts = list(executor.map(lambda _: self.finish(operation), range(16)))
        self.assertTrue(all(value == receipts[0] for value in receipts))
        self.assertEqual(receipts[0]['state'], 'added')
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.pool.accounts()), 1)

    def test_exchanging_marker_is_committed_before_provider_call(self):
        operation = self.start()
        original = self.provider.exchange_code
        def exchange(*args):
            with __import__('sqlite3').connect(self.path / 'pool.sqlite3') as observer:
                self.assertEqual(observer.execute('SELECT state FROM contributions WHERE id=?', (operation['contribution_id'],)).fetchone()[0], 'exchanging')
            return original(*args)
        self.provider.exchange_code = exchange
        self.assertEqual(self.finish(operation)['state'], 'added')

    def test_interrupted_exchange_is_terminal_and_never_replayed(self):
        operation = self.start()
        with self.pool.db:
            self.pool.db.execute("UPDATE contributions SET state='exchanging'")
        self.restart()
        self.assertEqual(self.finish(operation)['state'], 'uncertain')
        self.assertEqual(self.pool.contribution_cancel(self.first['device_token'], operation['contribution_id'])['state'], 'uncertain')
        self.assertEqual(self.provider.calls, [])
        self.assertIsNone(self.pool.db.execute('SELECT secret FROM contributions').fetchone()[0])

    def test_provider_failure_is_secret_free_terminal_and_never_replayed(self):
        operation = self.start()
        self.provider.failure = RuntimeError('sensitive-error-manual-auth-code-access-secret')
        receipt = self.finish(operation)
        self.assertEqual(receipt['state'], 'uncertain')
        self.assertEqual(self.pool.accounts(), [])
        self.restart()
        self.provider.failure = None
        self.assertEqual(self.finish(operation), receipt)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotIn(b'sensitive-error', (self.path / 'pool.sqlite3').read_bytes())

    def test_invalid_post_exchange_metadata_cannot_enter_pool(self):
        variants = []
        for field, value in [('subscriptionType', 'free'), ('rateLimitTier', '')]:
            grant = credentials()
            grant['account'][field] = value
            variants.append(grant)
        grant = credentials()
        grant['scopes'] = ['user:profile']
        variants.append(grant)
        grant = credentials()
        grant['expires_at'] = self.now + 1
        variants.append(grant)
        for grant in variants:
            self.provider.grant = grant
            operation = self.start()
            self.assertEqual(self.finish(operation)['state'], 'uncertain')
            self.assertEqual(self.pool.accounts(), [])

    def test_account_insert_and_receipt_failure_roll_back_together(self):
        operation = self.start()
        original = self.pool._finish_contribution
        def finish(row, state, account_id=None):
            if state == 'added':
                # Account INSERT has happened, but the transaction must roll it
                # back if writing its durable receipt fails.
                self.assertEqual(len(self.pool.accounts()), 1)
                raise RuntimeError('simulated receipt write failure')
            return original(row, state, account_id)
        with mock.patch.object(self.pool, '_finish_contribution', side_effect=finish):
            self.assertEqual(self.finish(operation)['state'], 'uncertain')
        self.assertEqual(self.pool.accounts(), [])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.finish(operation)['state'], 'uncertain')

    def test_pkce_and_credential_secrets_are_encrypted_or_absent_from_disk_and_receipts(self):
        operation = self.start()
        row = self.pool.db.execute('SELECT secret FROM contributions').fetchone()
        pkce = json.loads(self.pool.cipher.decrypt(row[0]))
        self.assertGreaterEqual(len(pkce['verifier']), 43)
        raw = (self.path / 'pool.sqlite3').read_bytes()
        for secret in (pkce['state'], pkce['verifier'], self.first['device_token']):
            self.assertNotIn(secret.encode(), raw)
        receipt = self.finish(operation)
        raw = (self.path / 'pool.sqlite3').read_bytes()
        for secret in (pkce['state'], pkce['verifier'], 'manual-auth-code', 'refresh-secret-donated', 'access-secret-donated', 'donated@example.test'):
            self.assertNotIn(secret.encode(), raw)
            self.assertNotIn(secret, json.dumps(receipt))
        self.assertIsNone(self.pool.db.execute('SELECT secret FROM contributions').fetchone()[0])

    def test_http_routes_authenticate_before_body_and_reject_extra_fields(self):
        server = PoolHTTPServer(('127.0.0.1', 0), self.pool)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def post(path, body, token=None, raw=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            try:
                headers = {'Content-Type': 'application/json'}
                if token:
                    headers['Authorization'] = 'Bearer ' + token
                connection.request('POST', path, raw if raw is not None else json.dumps(body), headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        token = self.first['device_token']
        try:
            prefix = '/v1/contributions/'
            for endpoint in ('start', 'complete', 'status', 'cancel'):
                self.assertEqual(post(prefix + endpoint, None, raw=b'not-json'), (401, {'error': 'unauthorized'}))
            status, operation = post(prefix + 'start', {}, token)
            self.assertEqual(status, 200)
            self.assertEqual(post(prefix + 'start', {'refresh_token': 'secret-upload'}, token), (400, {'error': 'invalid-body'}))
            self.assertEqual(post(prefix + 'complete', {'contribution_id': operation['contribution_id'], 'code': self.code(operation), 'account': credentials()}, token), (400, {'error': 'invalid-body'}))
            self.assertEqual(post(prefix + 'status', {}, token), (400, {'error': 'invalid-body'}))
            self.assertEqual(post(prefix + 'start', None, token, raw=b'{"unexpected":1,"unexpected":2}'), (400, {'error': 'invalid-body'}))
            self.assertEqual(post(prefix + 'start', None, token, raw=b'x' * 8193), (413, {'error': 'invalid-body-size'}))
            status, receipt = post(prefix + 'complete', {'contribution_id': operation['contribution_id'], 'code': self.code(operation)}, token)
            self.assertEqual(status, 200)
            self.assertEqual(receipt['state'], 'added')
            self.assertEqual(post(prefix + 'status', {'contribution_id': operation['contribution_id']}, token), (200, receipt))
            self.assertEqual(post(prefix + 'cancel', {'contribution_id': operation['contribution_id']}, token), (200, receipt))
            self.pool.revoke(self.first['device_id'])
            self.assertEqual(post(prefix + 'start', None, token, raw=b'not-json'), (401, {'error': 'unauthorized'}))
            self.assertEqual(len(self.provider.calls), 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
