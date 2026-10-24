"""Direct OAuth/usage transport; never receives or proxies inference.

Endpoint and OAuth grant conventions follow claude-swap's oauth.py (MIT,
Copyright (c) 2026 Onur Cetinkol); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import json
import math
import ssl
import time
import urllib.error
import urllib.request

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
MAX_RESPONSE = 1_048_576


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
            if value is None and key not in ("five_hour", "seven_day"):
                continue
            pct = value.get("utilization") if isinstance(value, dict) else None
            if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct) or pct < 0:
                raise ProviderError("bad-usage")
            result[key] = float(pct)
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
