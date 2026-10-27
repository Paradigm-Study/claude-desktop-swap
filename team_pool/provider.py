"""Direct OAuth/usage transport; never receives or proxies inference.

Refresh conventions follow claude-swap's oauth.py (MIT, Copyright (c) 2026
Onur Cetinkol); see THIRD_PARTY_NOTICES.md. Authorization-code endpoints, scopes,
and profile metadata mappings were checked against native Claude Code 2.1.300.
They are an observed interoperability contract, not a supported public API.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
MANUAL_REDIRECT_URL = "https://platform.claude.com/oauth/code/callback"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
MAX_RESPONSE = 1_048_576
# Native Claude Code 2.1.300's normal subscription sign-in scope set. Requesting
# these does not establish entitlement; use only scopes actually returned.
LOGIN_SCOPES = ("org:create_api_key", "user:profile", "user:inference",
                "user:sessions:claude_code", "user:mcp_servers",
                "user:file_upload", "user:plugins")
SUBSCRIPTION_TYPES = {"claude_pro": "pro", "claude_max": "max",
                      "claude_team": "team", "claude_enterprise": "enterprise"}


class ProviderError(Exception):
    """Only fixed diagnostic codes, never response bodies or credential values."""
    def __init__(self, code: str, retry_after: float = 0):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Provider:
    def __init__(self, *, clock=time.time, timeout=10, opener=None):
        self.clock, self.timeout = clock, timeout
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    @staticmethod
    def _validate_oauth_input(verifier, state, code=None):
        if (not isinstance(verifier, str)
                or re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) is None
                or not isinstance(state, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", state) is None):
            raise ProviderError("invalid-oauth-input")
        if code is not None and (not isinstance(code, str) or not 1 <= len(code) <= 8192
                or any(not 33 <= ord(char) <= 126 or char == "#" for char in code)):
            raise ProviderError("invalid-oauth-input")

    def authorization_url(self, verifier, state):
        """Build the provider's manual-code page; the verifier stays server-side."""
        self._validate_oauth_input(verifier, state)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        query = urllib.parse.urlencode({
            "code": "true", "client_id": CLIENT_ID, "response_type": "code",
            "redirect_uri": MANUAL_REDIRECT_URL, "scope": " ".join(LOGIN_SCOPES),
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        })
        return AUTHORIZE_URL + "?" + query

    def exchange_code(self, code, verifier, state):
        """Consume one code and verify its native subscription identity.

        The caller must validate the pasted callback state before calling this
        method. After input validation, every failure is potentially after code
        consumption: the coordinator must persist that boundary and never retry
        a grant whose result it could not commit.
        """
        if code is None:
            raise ProviderError("invalid-oauth-input")
        self._validate_oauth_input(verifier, state, code)
        data = self._request(TOKEN_URL, body={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": MANUAL_REDIRECT_URL, "client_id": CLIENT_ID,
            "code_verifier": verifier, "state": state,
        })
        access, refresh = data.get("access_token"), data.get("refresh_token")
        for token in (access, refresh):
            if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                    or any(not 33 <= ord(char) <= 126 for char in token)):
                raise ProviderError("incomplete-grant")
        expiry, scope = data.get("expires_in"), data.get("scope")
        if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry) or expiry <= 60
                or not math.isfinite(self.clock() + expiry)):
            raise ProviderError("incomplete-grant")
        if (not isinstance(scope, str) or len(scope) > 16384
                or not {"user:inference", "user:profile"}.issubset(scope.split())
                or any(len(part) > 256 or any(not 33 <= ord(char) <= 126 for char in part)
                       for part in scope.split())):
            raise ProviderError("incomplete-grant")
        token_type = data.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise ProviderError("incomplete-grant")
        expires_at = self.clock() + expiry
        profile = self._request(PROFILE_URL, token=access)
        account = self._profile_identity(profile)
        organization = profile["organization"]
        kind = organization.get("organization_type")
        subscription = SUBSCRIPTION_TYPES.get(kind) if isinstance(kind, str) else None
        tier = organization.get("rate_limit_tier")
        if not subscription or not self._metadata_string(tier):
            raise ProviderError("unverified-subscription")
        if not all(self._metadata_string(account.get(key))
                   for key in ("accountUuid", "organizationUuid", "emailAddress")):
            raise ProviderError("bad-profile")
        # Some token responses also identify the account. If present, that
        # independent identity must agree with the token-authenticated profile.
        for source, target in (("account", "accountUuid"), ("organization", "organizationUuid")):
            supplied = data.get(source)
            if supplied is not None and (not isinstance(supplied, dict)
                    or supplied.get("uuid") != account[target]):
                raise ProviderError("grant-profile-mismatch")
        account.update(subscriptionType=subscription, rateLimitTier=tier)
        return {"access_token": access, "refresh_token": refresh,
                "expires_at": expires_at, "scopes": scope.split(), "account": account}

    @staticmethod
    def _metadata_string(value):
        return (isinstance(value, str) and 1 <= len(value) <= 512
                and value == value.strip() and not any(ord(char) < 32 or ord(char) == 127 for char in value))

    def _request(self, url, *, token=None, body=None):
        headers = {"Accept": "application/json", "User-Agent": "claude-desktop-swap-pilot/1"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if url == USAGE_URL:
            headers["anthropic-beta"] = "oauth-2025-04-20"
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise ProviderError("bad-response")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ProviderError("bad-response")
            return value
        except urllib.error.HTTPError as exc:
            retry = 0
            try:
                retry = float(exc.headers.get("Retry-After", "0"))
                retry = min(86400, max(0, retry)) if math.isfinite(retry) else 0
            except (ValueError, TypeError, AttributeError):
                pass
            raise ProviderError("http-" + str(exc.code), retry) from None
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("transport-or-response-error") from None

    def identity(self, access_token):
        data = self._request(PROFILE_URL, token=access_token)
        return self._profile_identity(data)

    @staticmethod
    def _profile_identity(data):
        account, org = data.get("account"), data.get("organization")
        if not isinstance(account, dict) or not isinstance(org, dict):
            raise ProviderError("bad-profile")
        uuid, org_uuid = account.get("uuid"), org.get("uuid")
        if not isinstance(uuid, str) or not uuid or not isinstance(org_uuid, str) or not org_uuid:
            raise ProviderError("bad-profile")
        result = {"accountUuid": uuid, "organizationUuid": org_uuid}
        email = account.get("email") or account.get("email_address")
        if isinstance(email, str) and email:
            result["emailAddress"] = email
        return result

    def usage(self, access_token):
        raw = self._request(USAGE_URL, token=access_token)
        result = {}
        keys = {"five_hour", "seven_day"} | {k for k in raw if k.startswith("seven_day_")}
        for key in keys:
            value = raw.get(key)
            required = key in ("five_hour", "seven_day")
            if value is None and not required:
                continue
            # Weekly row aggregates describe consumption, not a quota window.
            # A reset-bearing or ambiguous object still fails closed below.
            if (not required and isinstance(value, dict)
                    and isinstance(value.get("rows"), list)
                    and "utilization" not in value and "resets_at" not in value):
                continue
            pct = value.get("utilization") if isinstance(value, dict) else None
            if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct) or pct < 0:
                raise ProviderError("bad-usage")
            result[key] = float(pct)
        # Valid observed quota windows can have is_active=False. Every per-model
        # weekly quota participates in model-all selection regardless of that
        # flag; spending/extra-usage limits deliberately do not.
        limits = raw.get("limits")
        if limits is not None:
            if not isinstance(limits, list):
                raise ProviderError("bad-usage")
            for limit in limits:
                if not isinstance(limit, dict):
                    raise ProviderError("bad-usage")
                if limit.get("kind") != "weekly_scoped":
                    continue
                scope = limit.get("scope")
                model = scope.get("model") if isinstance(scope, dict) else None
                name = model.get("display_name") if isinstance(model, dict) else None
                pct = limit.get("percent")
                if (limit.get("group") != "weekly" or not self._metadata_string(name)
                        or isinstance(pct, bool) or not isinstance(pct, (int, float))
                        or not math.isfinite(pct) or pct < 0):
                    raise ProviderError("bad-usage")
                key = "weekly_scoped:" + name.lower()
                result[key] = max(result.get(key, 0), float(pct))
        return result

    def refresh(self, credentials):
        data = self._request(TOKEN_URL, body={"grant_type": "refresh_token",
            "refresh_token": credentials["refresh_token"], "client_id": CLIENT_ID})
        access, refresh = data.get("access_token"), data.get("refresh_token")
        expiry = data.get("expires_in")
        # Missing successor refresh material is ambiguous for this rotating-token
        # pilot. Quarantine instead of silently replaying the consumed predecessor.
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise ProviderError("incomplete-refresh")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= 60:
            raise ProviderError("incomplete-refresh")
        scopes = data.get("scope")
        if not isinstance(scopes, str) or not scopes.split():
            raise ProviderError("incomplete-refresh")
        return {**credentials, "access_token": access, "refresh_token": refresh,
                "expires_at": self.clock() + expiry, "scopes": scopes.split()}
