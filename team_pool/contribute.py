"""Browser contribution using server-owned PKCE; never changes native credentials."""
from __future__ import annotations

import getpass
import re
import sys
import urllib.parse
import warnings
import webbrowser

from .client import ClientError, DEVICE_SECRET, RequestError, _number, _safe_string
from .provider import CLIENT_ID

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
CALLBACK_URL = "https://platform.claude.com/oauth/code/callback"
STATES = {"pending", "exchanging", "added", "duplicate", "failed", "uncertain", "expired", "cancelled"}
SCOPES = {"org:create_api_key", "user:profile", "user:inference", "user:sessions:claude_code",
          "user:mcp_servers", "user:file_upload", "user:plugins"}


def authorization_url(value):
    """Do not open an arbitrary server-supplied URL in a signed-in browser."""
    try:
        if not _safe_string(value, maximum=4096):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme, parsed.netloc, parsed.path) != ("https", "claude.com", "/cai/oauth/authorize") or parsed.fragment:
            raise ValueError
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
        expected = {"code", "client_id", "response_type", "redirect_uri", "scope",
                    "code_challenge", "code_challenge_method", "state"}
        if set(query) != expected or any(len(v) != 1 for v in query.values()):
            raise ValueError
        q = {key: values[0] for key, values in query.items()}
        if (q["code"] != "true" or q["client_id"] != CLIENT_ID or q["response_type"] != "code"
                or q["redirect_uri"] != CALLBACK_URL or q["code_challenge_method"] != "S256"
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", q["code_challenge"])
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", q["state"])
                or not {"user:profile", "user:inference"} <= set(q["scope"].split()) <= SCOPES):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ClientError("Coordinator returned an unrecognized Claude authorization URL; no browser was opened.") from None


def receipt(value, *, expected_id=None, allow_url=False):
    if not isinstance(value, dict):
        raise ClientError("Coordinator returned an invalid contribution receipt.")
    allowed = {"contribution_id", "state", "expires_at", "account_id"}
    if allow_url:
        allowed.add("authorization_url")
    if (set(value) - allowed or not _safe_string(value.get("contribution_id"), maximum=128)
            or value.get("state") not in STATES or not _number(value.get("expires_at"))
            or (expected_id is not None and value["contribution_id"] != expected_id)
            or (value["state"] in ("added", "duplicate") and not _safe_string(value.get("account_id"), maximum=64))
            or ("account_id" in value and not _safe_string(value["account_id"], maximum=64))):
        raise ClientError("Coordinator returned an invalid contribution receipt.")
    if allow_url and value["state"] == "pending":
        authorization_url(value.get("authorization_url"))
    elif "authorization_url" in value:
        raise ClientError("Coordinator returned an invalid contribution receipt.")
    return dict(value)


class Contribution:
    def __init__(self, client):
        self.client = client

    def _context(self):
        state = self.client._load()
        if state["state"] in ("revoked", "left"):
            raise ClientError("An active device enrollment is required to contribute an account.")
        token = self.client.native.get_secret(DEVICE_SECRET)
        if not _safe_string(token):
            raise ClientError("Device credential is missing. Enroll before contributing an account.")
        # Real provider authorization codes must never travel over test HTTP.
        if not state["server"].startswith("https://"):
            raise ClientError("Account contribution requires an HTTPS coordinator.")
        return state, token

    def _save(self, state, result):
        # The browser URL, authorization code and provider credentials are never persisted here.
        state["contribution"] = {key: result[key] for key in
                                 ("contribution_id", "state", "expires_at", "account_id") if key in result}
        self.client._save(state)

    def start(self):
        with self.client.locked():
            state, token = self._context()
            last = state.get("contribution")
            if last and last.get("state") in ("submitted", "exchanging"):
                raise ClientError("A contribution may have been submitted. Run contribute-account --status before starting another login.")
            result = receipt(self.client._transport(state).post("/v1/contributions/start", {}, token), allow_url=True)
            self._save(state, result)
            return result

    def inspect(self, *, cancel=False):
        with self.client.locked():
            state, token = self._context()
            last = state.get("contribution")
            if not last:
                return {"state": "none"}
            path = "/v1/contributions/" + ("cancel" if cancel else "status")
            result = receipt(self.client._transport(state).post(path, {"contribution_id": last["contribution_id"]}, token),
                             expected_id=last["contribution_id"])
            self._save(state, result)
            return result

    def complete(self, contribution_id, code):
        valid = isinstance(code, str) and len(code) <= 4225 and code.count("#") == 1
        raw, state_value = code.split("#", 1) if valid else ("", "")
        if not (valid and 1 <= len(raw) <= 4096 and all(33 <= ord(c) <= 126 for c in raw)
                and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", state_value)):
            raise ClientError("Paste the complete authorization code, including #state, at the hidden prompt.")
        with self.client.locked():
            state, token = self._context()
            last = state.get("contribution")
            if not last or last["contribution_id"] != contribution_id or last["state"] != "pending":
                raise ClientError("This login is not pending. Check contribute-account --status; no code was submitted.")
            state["contribution"]["state"] = "submitted"
            self.client._save(state)
            try:
                result = receipt(self.client._transport(state).post("/v1/contributions/complete",
                    {"contribution_id": contribution_id, "code": code}, token), expected_id=contribution_id)
            except RequestError:
                raise ClientError("Contribution delivery was not confirmed. Run contribute-account --status; do not resubmit the code.") from None
            self._save(state, result)
            return result


def hidden_code():
    if not sys.stdin.isatty():
        raise ClientError("Use an interactive terminal for account contribution; authorization codes cannot be passed in arguments or piped input.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass("Paste the complete authorization code (hidden): ").strip()
    except (getpass.GetPassWarning, EOFError):
        raise ClientError("A hidden terminal prompt is unavailable; the login remains pending.") from None


def run_contribution(client, *, no_browser=False, status=False, cancel=False,
                     open_browser=None, read_code=None, output=print):
    flow = Contribution(client)
    if status or cancel:
        return flow.inspect(cancel=cancel)
    if read_code is None and not sys.stdin.isatty():
        raise ClientError("Run contribute-account in an interactive terminal on your Mac.")
    output("This adds a subscription account to your team's shared pool. Team devices can use its quota and account permissions.")
    result = flow.start()
    if result["state"] != "pending":
        return result
    output("Sign into the account you intend to share on the Claude authorization page, then copy its complete code back here.")
    url = result["authorization_url"]
    opened = False
    if not no_browser:
        try:
            opened = (open_browser or webbrowser.open)(url, new=2)
        except Exception:
            pass
    if not opened:
        output("Open this Claude authorization URL: " + url)
    try:
        code = (read_code or hidden_code)()
        return flow.complete(result["contribution_id"], code)
    except KeyboardInterrupt:
        output("Contribution interrupted. Run contribute-account --status before continuing; --cancel only cancels a pending login.")
        raise
