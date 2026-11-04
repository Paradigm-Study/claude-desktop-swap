"""Enroll a Mac using its existing Claude Share sign-in, without an invitation.

The Share bearer goes only to the exact saved HTTPS origin. The coordinator gets
a short-lived opaque grant and proof of the Mac's challenge, never that bearer.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import secrets
import socket
import time
import urllib.error
import urllib.request

from .client import (ClientError, DEVICE_SECRET, Transport, _NoRedirect, _lock,
                     _number, _private_read, _safe_string, server_url)


DEFAULT_SHARE_SERVER = "https://claude-share.proud-limit-da0a.workers.dev"
DEFAULT_ACCOUNT_FILE = Path.home() / ".claude/shared-session/account.json"
IDENTITY = re.compile(r"[A-Za-z0-9_-]{8,40}")
OPAQUE = re.compile(r"[A-Za-z0-9_-]{32,256}")


def saved_token(path, origin):
    try:
        raw = _private_read(Path(path).expanduser())
        if len(raw) > 65536:
            raise ValueError
        saved = json.loads(raw).get("servers", {}).get(origin, {})
        token = saved.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"ssa_[A-Za-z0-9_-]{20,80}", token):
            raise ValueError
        return token
    except (OSError, ValueError, AttributeError, ClientError):
        raise ClientError("Sign in to Claude Share's Paradigm team with /team on this Mac first. Its saved sign-in must be a private, user-owned file.") from None


class ShareTransport:
    def __init__(self, origin):
        self.origin = server_url(origin)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def grant(self, token, challenge, device_name):
        request = urllib.request.Request(
            self.origin + "/api/native-pool/enrollment", method="POST",
            data=json.dumps({"challenge": challenge, "device_name": device_name}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "claude-desktop-swap-pilot/1"},
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 401:
                raise ClientError("Your Claude Share sign-in has expired. Sign in again with /team, then repeat setup.") from None
            if status == 403:
                raise ClientError("This Claude Share account is not a member of the team linked to the native pool.") from None
            raise ClientError(f"Claude Share could not authorize pool setup (HTTP {status}). No Share token was sent to the pool.") from None
        except Exception:
            raise ClientError("Claude Share authorization could not complete. Check connectivity and repeat setup; no credential values were logged.") from None


def _grant(value, now):
    try:
        if not isinstance(value, dict) or set(value) != {"grant", "expires_at", "pool_server", "team"}:
            raise ValueError
        if not isinstance(value["grant"], str) or not OPAQUE.fullmatch(value["grant"]):
            raise ValueError
        if not _number(value["expires_at"]) or not now < value["expires_at"] <= now + 300:
            raise ValueError
        team = value["team"]
        if (not isinstance(team, dict) or set(team) != {"id", "name"}
                or not isinstance(team["id"], str) or not IDENTITY.fullmatch(team["id"])
                or not _safe_string(team["name"], maximum=60)):
            raise ValueError
        origin = server_url(value["pool_server"])
        if origin != value["pool_server"]:
            raise ValueError
        return origin, team
    except (ValueError, TypeError, ClientError):
        raise ClientError("Claude Share returned an invalid pool authorization. No pool enrollment was attempted.") from None


def enroll_from_share(client, *, share_server=DEFAULT_SHARE_SERVER,
                      account_file=DEFAULT_ACCOUNT_FILE, device_name=None, link=False,
                      share_transport=None, pool_transport=None, clock=time.time):
    origin = server_url(share_server)
    name = device_name if device_name is not None else socket.gethostname()
    if not _safe_string(name, maximum=80):
        raise ClientError("Use a device name between 1 and 80 characters without control characters.")
    with _lock(client.config):
        state = client._load(optional=True)
        if link:
            if not state or state["state"] in ("revoked", "left"):
                raise ClientError("An existing active pool enrollment is required for link-share.")
        elif state is not None:
            raise ClientError("This Mac is already enrolled. Use link-share to attach its existing device to Claude Share membership.")
        client._preflight(state)
        # Only the saved token for this exact origin is used. Cached account,
        # team selection, email and display name are never authorization proof.
        bearer = saved_token(account_file, origin)
        verifier = secrets.token_urlsafe(32)
        challenge = hashlib.sha256(verifier.encode()).hexdigest()
        value = (share_transport or ShareTransport(origin)).grant(bearer, challenge, name)
        pool, team = _grant(value, clock())
        if link and pool != state["server"]:
            raise ClientError("Claude Share is linked to another pool; this Mac's enrollment was preserved.")
        device_token = None
        if link:
            device_token = client.native.get_secret(DEVICE_SECRET)
            if not _safe_string(device_token):
                raise ClientError("The existing device credential is missing. Preserve recovery state before enrolling again.")
        transport = pool_transport or (client._transport(state) if link else Transport(pool))
        response = transport.post("/v1/link/share" if link else "/v1/enroll/share",
                                  {"grant": value["grant"], "verifier": verifier, "device_name": name},
                                  token=device_token)
        if (not isinstance(response, dict)
                or not _safe_string(response.get("device_id"), maximum=256)
                or not isinstance(response.get("share_account_id"), str)
                or not IDENTITY.fullmatch(response["share_account_id"])
                or response.get("share_team_id") != team["id"]):
            raise ClientError("Pool enrollment returned an invalid identity. No native credentials were changed.")
        if link:
            if response["device_id"] != state["device_id"]:
                raise ClientError("Pool linking returned another device; local enrollment was preserved.")
        else:
            if not isinstance(response.get("device_token"), str) or not OPAQUE.fullmatch(response["device_token"]):
                raise ClientError("Pool enrollment returned an invalid device credential.")
            state = {"version": 1, "server": pool, "allow_loopback_http": False,
                     "device_id": response["device_id"], "state": "enrolled", "managed": False}
            client.native.set_secret(DEVICE_SECRET, response["device_token"])
        state.update(share_server=origin, share_team_id=team["id"], share_team_name=team["name"],
                     share_account_id=response["share_account_id"])
        try:
            client._save(state)
        except Exception:
            if not link:
                client.native.delete_secret(DEVICE_SECRET)
            raise
        return {**client._status(state), "share_team": team["name"], "membership_linked": True}
