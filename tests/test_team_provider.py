import io
import json
import ssl
import unittest
import urllib.error
from team_pool.provider import Provider, ProviderError, NoRedirect, TOKEN_URL, USAGE_URL


class Response(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, value):
        self.value = value
        self.requests = []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.value, Exception):
            raise self.value
        return Response(json.dumps(self.value).encode())


class ProviderTest(unittest.TestCase):
    def test_usage_validates_base_and_scoped_windows(self):
        opener = FakeOpener({'five_hour': {'utilization': 20}, 'seven_day': {'utilization': 30},
                             'seven_day_opus': {'utilization': 70}, 'seven_day_sonnet': None,
                             'extra_usage': {'is_enabled': True}})
        result = Provider(opener=opener).usage('access-secret')
        self.assertEqual(result, {'five_hour': 20, 'seven_day': 30, 'seven_day_opus': 70})
        request = opener.requests[0][0]
        self.assertEqual(request.full_url, USAGE_URL)
        self.assertEqual(request.get_header('Authorization'), 'Bearer access-secret')

    def test_usage_missing_or_nan_is_unknown(self):
        for body in ({}, {'five_hour': {'utilization': float('nan')}, 'seven_day': {'utilization': 1}},
                     {'five_hour': {'utilization': True}, 'seven_day': {'utilization': 1}}):
            with self.subTest(body=body), self.assertRaises(ProviderError):
                Provider(opener=FakeOpener(body)).usage('secret')

    def test_refresh_requires_successor_and_preserves_imported_fields(self):
        opener = FakeOpener({'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 1000,
                             'scope': 'user:inference user:profile'})
        source = {'access_token': 'old-access', 'refresh_token': 'old-refresh', 'scopes': ['old'], 'account': {}}
        result = Provider(opener=opener, clock=lambda: 500).refresh(source)
        self.assertEqual(result['expires_at'], 1500)
        self.assertEqual(result['refresh_token'], 'new-refresh')
        request = opener.requests[0][0]
        self.assertEqual(request.full_url, TOKEN_URL)
        self.assertEqual(json.loads(request.data)['refresh_token'], 'old-refresh')
        self.assertEqual(source['refresh_token'], 'old-refresh')
        with self.assertRaises(ProviderError):
            Provider(opener=FakeOpener({'access_token': 'only-access'})).refresh(source)

    def test_error_body_and_url_never_in_exception(self):
        error = urllib.error.HTTPError('https://example.test/secret-token', 429, 'secret-message',
                                       {'Retry-After': '123'}, io.BytesIO(b'secret-response'))
        with self.assertRaises(ProviderError) as exc:
            Provider(opener=FakeOpener(error)).usage('secret-bearer')
        self.assertEqual(str(exc.exception), 'http-429')
        self.assertEqual(exc.exception.retry_after, 123)

    def test_redirects_are_refused_and_tls_verification_enabled(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, None, {}, 'https://evil.test'))
        provider = Provider()
        handlers = provider.opener.handlers
        https = next(handler for handler in handlers if isinstance(handler, __import__('urllib.request', fromlist=['HTTPSHandler']).HTTPSHandler))
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)

    def test_profile_identity_requires_organization(self):
        with self.assertRaises(ProviderError):
            Provider(opener=FakeOpener({'account': {'uuid': 'a'}})).identity('secret')
        result = Provider(opener=FakeOpener({'account': {'uuid': 'a', 'email': 'a@example.test'},
                                            'organization': {'uuid': 'org-a'}})).identity('secret')
        self.assertEqual(result['organizationUuid'], 'org-a')


if __name__ == '__main__':
    unittest.main()
