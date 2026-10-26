import io
import json
import ssl
import unittest
import urllib.error
import urllib.parse
from team_pool.provider import (Provider, ProviderError, NoRedirect, TOKEN_URL,
    USAGE_URL, PROFILE_URL, AUTHORIZE_URL, MANUAL_REDIRECT_URL, CLIENT_ID, LOGIN_SCOPES,
    MAX_RESPONSE)


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


class SequenceOpener(FakeOpener):
    def __init__(self, values):
        super().__init__(None)
        self.values = iter(values)

    def open(self, request, timeout):
        self.value = next(self.values)
        return super().open(request, timeout)


class ProviderTest(unittest.TestCase):
    VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    STATE = "s" * 43

    @staticmethod
    def grant(**updates):
        return {"access_token": "new-access", "refresh_token": "new-refresh",
                "expires_in": 3600, "token_type": "Bearer",
                "scope": "user:profile user:inference user:sessions:claude_code", **updates}

    @staticmethod
    def profile(**organization_updates):
        return {"account": {"uuid": "account-a", "email": "a@example.test"},
                "organization": {"uuid": "org-a", "organization_type": "claude_max",
                                 "rate_limit_tier": "default_claude_max_20x", **organization_updates}}

    def test_authorization_url_uses_pkce_s256_and_native_manual_callback(self):
        opener = FakeOpener(None)
        url = Provider(opener=opener).authorization_url(self.VERIFIER, self.STATE)
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), AUTHORIZE_URL)
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {
            "code": ["true"], "client_id": [CLIENT_ID], "response_type": ["code"],
            "redirect_uri": [MANUAL_REDIRECT_URL], "scope": [" ".join(LOGIN_SCOPES)],
            "code_challenge": ["E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"],
            "code_challenge_method": ["S256"], "state": [self.STATE],
        })
        self.assertNotIn(self.VERIFIER, url)
        self.assertEqual(opener.requests, [])

    def test_invalid_oauth_inputs_reject_before_any_network_call(self):
        invalid = [("code", "short", self.STATE), ("code", self.VERIFIER, "short"),
                   ("code", "v" * 129, self.STATE), ("code", self.VERIFIER, "s" * 129),
                   ("code", "é" * 43, self.STATE), ("code", self.VERIFIER, "s/" * 22),
                   ("code#state", self.VERIFIER, self.STATE), ("", self.VERIFIER, self.STATE),
                   (None, self.VERIFIER, self.STATE), ("code\n", self.VERIFIER, self.STATE),
                   ("code\x00", self.VERIFIER, self.STATE), ("é", self.VERIFIER, self.STATE),
                   ("c" * 8193, self.VERIFIER, self.STATE)]
        for args in invalid:
            opener = FakeOpener(None)
            with self.subTest(args=args), self.assertRaisesRegex(ProviderError, "^invalid-oauth-input$"):
                Provider(opener=opener).exchange_code(*args)
            self.assertEqual(opener.requests, [])

    def test_exchange_verifies_native_profile_and_preserves_real_entitlement(self):
        for organization_type, subscription in (("claude_pro", "pro"), ("claude_max", "max"),
                                                 ("claude_team", "team"), ("claude_enterprise", "enterprise")):
            opener = SequenceOpener([self.grant(), self.profile(organization_type=organization_type)])
            result = Provider(opener=opener, clock=lambda: 500).exchange_code("one-time-code", self.VERIFIER, self.STATE)
            self.assertEqual(result, {"access_token": "new-access", "refresh_token": "new-refresh",
                "expires_at": 4100, "scopes": ["user:profile", "user:inference", "user:sessions:claude_code"],
                "account": {"accountUuid": "account-a", "emailAddress": "a@example.test",
                    "organizationUuid": "org-a", "subscriptionType": subscription,
                    "rateLimitTier": "default_claude_max_20x"}})
            grant, profile = (pair[0] for pair in opener.requests)
            self.assertEqual(grant.full_url, TOKEN_URL)
            self.assertEqual(json.loads(grant.data), {"grant_type": "authorization_code", "code": "one-time-code",
                "redirect_uri": MANUAL_REDIRECT_URL, "client_id": CLIENT_ID,
                "code_verifier": self.VERIFIER, "state": self.STATE})
            self.assertEqual(profile.full_url, PROFILE_URL)
            self.assertEqual(profile.get_header("Authorization"), "Bearer new-access")
            self.assertEqual(grant.get_header("User-agent"), "claude-desktop-swap-pilot/1")
            self.assertNotIn("one-time-code", grant.full_url)
            self.assertNotIn("new-access", profile.full_url)

    def test_exchange_requires_refresh_scopes_and_finite_usable_expiry(self):
        changes = [{"refresh_token": None}, {"access_token": "token\nsecret"}, {"refresh_token": "token\x00secret"},
                   {"expires_in": True}, {"expires_in": 60}, {"expires_in": float("inf")},
                   {"expires_in": float("nan")}, {"scope": "user:inference"}, {"scope": "user:profile"},
                   {"scope": "user:profile user:inference invalid\x00scope"}, {"token_type": "mac"}]
        for change in changes:
            opener = SequenceOpener([self.grant(**change), self.profile()])
            with self.subTest(change=change), self.assertRaisesRegex(ProviderError, "^incomplete-grant$"):
                Provider(opener=opener).exchange_code("code", self.VERIFIER, self.STATE)
            self.assertEqual(len(opener.requests), 1)

    def test_inference_scope_alone_never_invents_subscription_metadata(self):
        changes = [{"organization_type": None}, {"organization_type": "api"}, {"organization_type": "claude_free"},
                   {"organization_type": []}, {"organization_type": "new_unknown_plan"},
                   {"rate_limit_tier": None}, {"rate_limit_tier": ""}, {"rate_limit_tier": " default "},
                   {"rate_limit_tier": "default\nsecret"}]
        for change in changes:
            opener = SequenceOpener([self.grant(), self.profile(**change)])
            with self.subTest(change=change), self.assertRaisesRegex(ProviderError, "^unverified-subscription$"):
                Provider(opener=opener).exchange_code("code", self.VERIFIER, self.STATE)

    def test_exchange_requires_complete_verified_identity(self):
        for profile in ({"account": {}, "organization": {}},
                        {**self.profile(), "account": {"uuid": "account-a"}},
                        {**self.profile(), "account": {"uuid": "account-a", "email": "a\n@example.test"}}):
            opener = SequenceOpener([self.grant(), profile])
            with self.subTest(profile=profile), self.assertRaisesRegex(ProviderError, "^bad-profile$"):
                Provider(opener=opener).exchange_code("code", self.VERIFIER, self.STATE)

    def test_exchange_cross_checks_optional_grant_identity(self):
        for extra in ({"account": {"uuid": "other"}}, {"organization": {"uuid": "other"}},
                      {"account": {}}, {"organization": "org-a"}):
            opener = SequenceOpener([self.grant(**extra), self.profile()])
            with self.subTest(extra=extra), self.assertRaisesRegex(ProviderError, "^grant-profile-mismatch$"):
                Provider(opener=opener).exchange_code("code", self.VERIFIER, self.STATE)
        opener = SequenceOpener([self.grant(account={"uuid": "account-a"}, organization={"uuid": "org-a"}), self.profile()])
        self.assertEqual(Provider(opener=opener).exchange_code("code", self.VERIFIER, self.STATE)["account"]["accountUuid"], "account-a")

    def test_exchange_does_not_retry_or_leak_consumed_code_on_transport_failure(self):
        failure = urllib.error.URLError("secret-response-with-code")
        opener = SequenceOpener([failure])
        with self.assertRaisesRegex(ProviderError, "^transport-or-response-error$"):
            Provider(opener=opener).exchange_code("secret-code", self.VERIFIER, self.STATE)
        self.assertEqual(len(opener.requests), 1)

    def test_provider_bounds_response_reads(self):
        class OversizedOpener:
            def open(inner, request, timeout):
                return Response(b" " * (MAX_RESPONSE + 1))
        with self.assertRaisesRegex(ProviderError, "^bad-response$"):
            Provider(opener=OversizedOpener()).exchange_code("code", self.VERIFIER, self.STATE)

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

    def test_weekly_row_aggregates_are_metadata_not_quota_windows(self):
        aggregate = {'as_of': '2026-10-26T12:00:00Z', 'rows': [],
                     'window_started_at': '2026-10-20T12:00:00Z'}
        opener = FakeOpener({'five_hour': {'utilization': 40}, 'seven_day': {'utilization': 78},
            'seven_day_opus': None, 'seven_day_sonnet': None,
            'seven_day_breakdown': aggregate,
            'seven_day_other_aggregate': {**aggregate, 'rows': [{'percent': 99}]},
            'extra_usage': {'is_enabled': True, 'utilization': 100}})
        self.assertEqual(Provider(opener=opener).usage('access'), {'five_hour': 40, 'seven_day': 78})

    def test_metadata_classification_does_not_hide_malformed_quota_windows(self):
        base = {'five_hour': {'utilization': 40}, 'seven_day': {'utilization': 78}}
        for change in ({'five_hour': {'rows': []}}, {'seven_day': {'rows': []}},
                       {'seven_day_opus': {}},
                       {'seven_day_opus': {'resets_at': '2026-10-27T00:00:00Z'}},
                       {'seven_day_opus': {'rows': [], 'utilization': None}},
                       {'seven_day_opus': {'rows': [], 'utilization': '70'}},
                       {'seven_day_opus': {'rows': [], 'resets_at': None}},
                       {'seven_day_breakdown': {'rows': 'invalid'}}):
            with self.subTest(change=change), self.assertRaisesRegex(ProviderError, '^bad-usage$'):
                Provider(opener=FakeOpener({**base, **change})).usage('access')
        result = Provider(opener=FakeOpener({**base,
            'seven_day_opus': {'rows': [], 'utilization': 95}})).usage('access')
        self.assertEqual(result['seven_day_opus'], 95)

    def test_current_limits_include_nonbinding_model_quotas_but_not_spend(self):
        scoped = {'kind': 'weekly_scoped', 'group': 'weekly', 'is_active': False, 'percent': 0,
                  'scope': {'model': {'id': None, 'display_name': 'Fable'}, 'surface': None}}
        body = {'five_hour': {'utilization': 40}, 'seven_day': {'utilization': 78},
            'seven_day_breakdown': {'as_of': '2026-10-26T12:00:00Z', 'rows': [],
                                    'window_started_at': '2026-10-20T12:00:00Z'},
            'limits': [
                {'kind': 'session', 'group': 'session', 'is_active': False, 'percent': 40, 'scope': None},
                {'kind': 'weekly_all', 'group': 'weekly', 'is_active': True, 'percent': 78, 'scope': {}},
                scoped,
                {'kind': 'extra_usage', 'group': 'spend', 'percent': 100,
                 'scope': {'model': {'display_name': 'Fable'}}}],
            'extra_usage': {'is_enabled': True, 'utilization': 100}}
        self.assertEqual(Provider(opener=FakeOpener(body)).usage('access'), {
            'five_hour': 40, 'seven_day': 78, 'weekly_scoped:fable': 0})
        body['limits'].extend([{**scoped, 'percent': 95},
            {**scoped, 'percent': 10, 'scope': {'model': {'display_name': 'FABLE'}}}])
        self.assertEqual(Provider(opener=FakeOpener(body)).usage('access')['weekly_scoped:fable'], 95)

    def test_malformed_scoped_quotas_fail_closed(self):
        scoped = {'kind': 'weekly_scoped', 'group': 'weekly', 'is_active': False, 'percent': 0,
                  'scope': {'model': {'display_name': 'Fable'}}}
        base = {'five_hour': {'utilization': 40}, 'seven_day': {'utilization': 78}}
        for change in ({'percent': None}, {'percent': True}, {'percent': '95'}, {'percent': -1},
                       {'percent': float('nan')}, {'percent': float('inf')},
                       {'group': None}, {'group': 'spend'}, {'scope': None}, {'scope': {}},
                       {'scope': {'model': {'display_name': ''}}},
                       {'scope': {'model': {'display_name': 'Fable\n'}}}):
            with self.subTest(change=change), self.assertRaisesRegex(ProviderError, '^bad-usage$'):
                Provider(opener=FakeOpener({**base, 'limits': [{**scoped, **change}]})).usage('access')
        for limits in ({}, 'unknown', [None]):
            with self.subTest(limits=limits), self.assertRaisesRegex(ProviderError, '^bad-usage$'):
                Provider(opener=FakeOpener({**base, 'limits': limits})).usage('access')
        self.assertEqual(Provider(opener=FakeOpener({**base, 'limits': None})).usage('access'),
                         {'five_hour': 40, 'seven_day': 78})

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
