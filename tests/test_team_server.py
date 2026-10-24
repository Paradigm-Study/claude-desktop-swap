import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from team_pool.provider import ProviderError
from team_pool.server import Coordinator, PoolError, PoolHTTPServer, main


def credential(name='a', expiry=10000):
    return {'access_token': 'access-secret-' + name, 'refresh_token': 'refresh-secret-' + name,
            'expires_at': expiry, 'scopes': ['user:inference', 'user:profile'],
            'account': {'accountUuid': name, 'emailAddress': name + '@example.test',
                        'organizationUuid': 'org-' + name, 'subscriptionType': 'max',
                        'rateLimitTier': 'default_claude_max_20x'}}


class FakeProvider:
    def __init__(self):
        self.quotas = {'a': 10, 'b': 20}
        self.usage_calls = 0
        self.refresh_calls = 0
        self.failure = None

    def identity(self, access):
        name = access.rsplit('-', 1)[-1]
        return {'accountUuid': name, 'organizationUuid': 'org-' + name}

    def usage(self, access):
        self.usage_calls += 1
        if self.failure:
            raise self.failure
        return {'five_hour': self.quotas[access.rsplit('-', 1)[-1]], 'seven_day': 10}

    def refresh(self, value):
        self.refresh_calls += 1
        if self.failure:
            raise self.failure
        return {**value, 'access_token': 'renewed-' + value['account']['accountUuid'],
                'refresh_token': 'renewed-refresh-' + value['account']['accountUuid'], 'expires_at': 20000}


class CoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state'
        self.provider = FakeProvider()
        self.now = 1000
        self.pool = Coordinator(self.path, provider=self.provider, clock=lambda: self.now, create=True)

    def tearDown(self):
        self.pool.close()
        self.temp.cleanup()

    def device(self):
        return self.pool.enroll(self.pool.invite(), 'Test Mac')['device_token']

    def add(self, name='a', expiry=10000):
        self.pool.add_account(name, credential(name, expiry), exclusive_owner=True)

    def test_import_requires_real_inference_scope(self):
        value = credential()
        value['scopes'] = ['user:profile']
        with self.assertRaises(PoolError):
            self.pool.add_account('a', value, exclusive_owner=True)
        self.assertEqual(self.pool.accounts(), [])

    def test_single_use_invitation_concurrent_replay(self):
        invitation = self.pool.invite()
        def redeem(_):
            try:
                return self.pool.enroll(invitation, 'test')
            except PoolError:
                return None
        with concurrent.futures.ThreadPoolExecutor(8) as executor:
            results = list(executor.map(redeem, range(16)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(len(self.pool.devices()), 1)

    def test_expired_invitation(self):
        invitation = self.pool.invite(30)
        self.now += 31
        with self.assertRaises(PoolError):
            self.pool.enroll(invitation, 'test')

    def test_assignment_is_sticky_and_balances_new_devices(self):
        self.add('a')
        self.add('b')
        first, second = self.device(), self.device()
        self.assertEqual(self.pool.assignment(first)['account_id'], 'a')
        self.assertEqual(self.pool.assignment(first)['account_id'], 'a')
        self.assertEqual(self.pool.assignment(second)['account_id'], 'b')
        self.assertEqual(self.provider.usage_calls, 2)

    def test_rotation_on_threshold_and_no_refresh_leak(self):
        self.add('a')
        self.add('b')
        token = self.device()
        original = self.pool.assignment(token)
        self.assertEqual(original['account_id'], 'a')
        self.provider.quotas['a'] = 90
        self.now += 121
        updated = self.pool.assignment(token)
        self.assertEqual(updated['account_id'], 'b')
        self.assertNotIn('refresh', json.dumps(updated))
        self.assertEqual(set(updated['account']), set(credential()['account']))

    def test_stale_usage_preserves_current_but_refuses_new_device(self):
        self.add()
        token = self.device()
        self.pool.assignment(token)
        self.provider.failure = ProviderError('http-429', 600)
        self.now += 301
        result = self.pool.assignment(token)
        self.assertEqual(result['state'], 'quota_stale')
        calls = self.provider.usage_calls
        self.now += 10
        self.pool.assignment(token)
        self.assertEqual(self.provider.usage_calls, calls)
        with self.assertRaises(PoolError) as exc:
            self.pool.assignment(self.device())
        self.assertEqual(exc.exception.status, 503)

    def test_all_exhausted_current_is_explicit_new_device_refused(self):
        self.add()
        token = self.device()
        self.pool.assignment(token)
        self.provider.quotas['a'] = 100
        self.now += 121
        self.assertEqual(self.pool.assignment(token)['state'], 'quota_exhausted')
        with self.assertRaises(PoolError):
            self.pool.assignment(self.device())

    def test_scope_window_exhaustion_prevents_assignment(self):
        self.add()
        self.provider.usage = lambda _: {'five_hour': 1, 'seven_day': 1, 'seven_day_sonnet': 91}
        with self.assertRaises(PoolError):
            self.pool.assignment(self.device())

    def test_revoke_blocks_existing_token_without_provider_call(self):
        self.add()
        enrollment = self.pool.enroll(self.pool.invite(), 'test')
        self.pool.assignment(enrollment['device_token'])
        self.pool.revoke(enrollment['device_id'])
        calls = self.provider.usage_calls
        with self.assertRaises(PoolError) as exc:
            self.pool.assignment(enrollment['device_token'])
        self.assertEqual(exc.exception.status, 401)
        self.assertEqual(self.provider.usage_calls, calls)

    def test_secrets_encrypted_and_device_invitation_stored_as_hashes(self):
        self.add()
        invitation = self.pool.invite()
        enrollment = self.pool.enroll(invitation, 'test')
        self.pool.assignment(enrollment['device_token'])
        raw = (self.path / 'pool.sqlite3').read_bytes()
        for value in ('access-secret-a', 'refresh-secret-a', invitation, enrollment['device_token'], 'a@example.test'):
            self.assertNotIn(value.encode(), raw)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o700)
        for filename in ('pool.sqlite3', 'master.key', 'writer.lock'):
            self.assertEqual(os.stat(self.path / filename).st_mode & 0o777, 0o600)

    def test_process_lock_excludes_other_admin_or_server(self):
        with self.assertRaises(PoolError) as exc:
            Coordinator(self.path, provider=self.provider)
        self.assertIn('already-running', exc.exception.code)

    def test_successful_refresh_issued_once_and_persisted(self):
        self.add(expiry=1200)
        token = self.device()
        result = self.pool.assignment(token)
        self.assertEqual(result['generation'], 2)
        self.assertEqual(result['access_token'], 'renewed-a')
        self.assertEqual(self.provider.refresh_calls, 1)
        self.pool.assignment(token)
        self.assertEqual(self.provider.refresh_calls, 1)
        self.pool.close()
        self.pool = Coordinator(self.path, provider=self.provider, clock=lambda: self.now)
        self.assertEqual(self.pool.assignment(token)['generation'], 2)
        self.assertEqual(self.provider.refresh_calls, 1)

    def test_uncertain_refresh_quarantines_across_restart(self):
        self.add(expiry=1200)
        token = self.device()
        self.provider.failure = RuntimeError('secret-provider-error-access-secret-a')
        with self.assertRaises(PoolError):
            self.pool.assignment(token)
        self.assertEqual(self.provider.refresh_calls, 1)
        self.assertEqual(self.pool.accounts()[0]['state'], 'quarantined')
        self.pool.close()
        self.provider.failure = None
        self.pool = Coordinator(self.path, provider=self.provider, clock=lambda: self.now)
        with self.assertRaises(PoolError):
            self.pool.assignment(token)
        self.assertEqual(self.provider.refresh_calls, 1)
        self.assertNotIn(b'secret-provider-error', (self.path / 'pool.sqlite3').read_bytes())

    def test_interrupted_durable_refresh_never_replayed(self):
        self.add()
        with self.pool.db:
            self.pool.db.execute("UPDATE accounts SET state='refreshing'")
        self.pool.close()
        self.pool = Coordinator(self.path, provider=self.provider, clock=lambda: self.now)
        self.pool.poll()
        self.assertEqual(self.pool.accounts()[0]['state'], 'quarantined')
        self.assertEqual(self.provider.refresh_calls, 0)

    def test_identity_mismatch_quarantines_before_delivery(self):
        self.add()
        self.provider.identity = lambda _: {'accountUuid': 'wrong', 'organizationUuid': 'org-a'}
        with self.assertRaises(PoolError):
            self.pool.assignment(self.device())
        self.assertEqual(self.pool.accounts()[0]['state'], 'quarantined')

    def test_import_requires_exclusive_owner_and_unique_account(self):
        with self.assertRaises(PoolError):
            self.pool.add_account('a', credential())
        self.add()
        with self.assertRaises(PoolError):
            self.pool.add_account('b', credential(), exclusive_owner=True)

    def test_explicit_quarantine_recovery_requires_same_identity_fresh_lineage(self):
        self.add(expiry=1200)
        token = self.device()
        self.provider.failure = ProviderError('transport-or-response-error')
        with self.assertRaises(PoolError):
            self.pool.assignment(token)
        with self.assertRaises(PoolError):
            self.pool.add_account('a', credential(), exclusive_owner=True, replace_quarantined=True)
        with self.assertRaises(PoolError):
            self.pool.add_account('a', credential('b'), exclusive_owner=True, replace_quarantined=True)
        recovered = credential()
        recovered['refresh_token'] = 'newly-authenticated-lineage'
        self.pool.add_account('a', recovered, exclusive_owner=True, replace_quarantined=True)
        self.provider.failure = None
        self.assertEqual(self.pool.assignment(token)['generation'], 2)
        with self.assertRaises(PoolError):
            self.pool.add_account('a', recovered, exclusive_owner=True, replace_quarantined=True)

    def test_growing_database_is_not_limited_like_credential_import(self):
        with self.pool.db:
            self.pool.db.execute('INSERT INTO audit VALUES (?,?,?)', (1000, 'fixture', 'x' * 1_100_000))
        self.pool.close()
        self.pool = Coordinator(self.path, provider=self.provider, clock=lambda: self.now)
        self.assertEqual(self.pool.devices(), [])

    def test_http_enrollment_assignment_revoke_generic_errors(self):
        self.add()
        server = PoolHTTPServer(('127.0.0.1', 0), self.pool)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = 'http://127.0.0.1:' + str(server.server_port)
        def post(path, body, token=None):
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            request = urllib.request.Request(url + path, json.dumps(body).encode(), headers)
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as response:
                return response.code, json.load(response)
        try:
            status, value = post('/v1/enroll', {'invitation': self.pool.invite(), 'device_name': 'test'})
            self.assertEqual(status, 200)
            token = value['device_token']
            self.assertEqual(post('/v1/assignment', {}, token)[0], 200)
            self.pool.revoke(value['device_id'])
            self.assertEqual(post('/v1/assignment', {}, token), (401, {'error': 'unauthorized'}))
            self.assertEqual(post('/v1/assignment', {'secret': 'do-not-echo'}, token), (400, {'error': 'invalid-body'}))
            self.assertEqual(post('/v1/enroll', {'invitation': 's' * 40, 'device_name': 'test'})[0], 401)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_nonloopback_requires_tls(self):
        self.pool.close()
        with contextlib.redirect_stderr(io.StringIO()) as output:
            result = main(['--state', str(self.path), 'serve', '--host', '0.0.0.0'])
        self.assertEqual(result, 1)
        self.assertIn('requires-tls', output.getvalue())


if __name__ == '__main__':
    unittest.main()
