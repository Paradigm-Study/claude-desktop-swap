#!/usr/bin/env python3
"""Administer a deployed native pool; keep all bearer tokens out of arguments."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request


class AdminError(Exception):
    pass


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise AdminError("Input must be a user-owned regular file with mode 0600.")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise AdminError("Input is too large.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AdminError("Expected a JSON object.")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Admin:
    def __init__(self, configuration):
        server, token = configuration.get("server"), configuration.get("admin_token")
        if not isinstance(server, str) or not isinstance(token, str):
            raise AdminError("Admin configuration requires server and admin_token.")
        url = urllib.parse.urlsplit(server)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.path not in ("", "/")
                or "?" in server or "#" in server or "\\" in server
                or any(c.isspace() for c in server)
                or not 32 <= len(token) <= 256 or any(not 33 <= ord(c) <= 126 for c in token)):
            raise AdminError("Invalid HTTPS coordinator origin or admin credential.")
        self.server, self.token = server.rstrip("/"), token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def post(self, path, body):
        request = urllib.request.Request(self.server + path, method="POST",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
            "Authorization": "Bearer " + self.token, "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=75) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise AdminError("Coordinator response is too large.")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise AdminError(f"Coordinator rejected the request (HTTP {status}).") from None
        except AdminError:
            raise
        except Exception:
            raise AdminError("Coordinator request failed; no credential values were logged.") from None

    def invite(self, destination, ttl):
        # Reserve the filename before creating a remote invitation. Never overwrite.
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                value = self.post("/admin/invites", {"ttl": ttl})
                token = value.get("invitation")
                if not isinstance(token, str) or not 32 <= len(token) <= 256 or any(c.isspace() for c in token):
                    raise AdminError("Coordinator returned an invalid invitation.")
                stream.write(token + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return {"invitation_file": str(destination), "expires_at": value.get("expires_at")}
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
        default=Path.home() / ".config/claude-desktop-swap/cloudflare-admin.json")
    commands = parser.add_subparsers(dest="command", required=True)
    invite = commands.add_parser("invite")
    invite.add_argument("--out", type=Path, required=True)
    invite.add_argument("--ttl", type=int, default=86400)
    commands.add_parser("accounts")
    commands.add_parser("devices")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("device_id")
    add = commands.add_parser("import-account")
    add.add_argument("--id", required=True)
    add.add_argument("--credentials", type=Path, required=True)
    add.add_argument("--exclusive-owner", action="store_true", required=True)
    add.add_argument("--replace-quarantined", action="store_true")
    args = parser.parse_args(argv)
    try:
        admin = Admin(private_json(args.config.expanduser()))
        if args.command == "invite":
            if not 30 <= args.ttl <= 86400:
                raise AdminError("Invitation TTL must be between 30 and 86400 seconds.")
            result = admin.invite(args.out.expanduser().absolute(), args.ttl)
        elif args.command in ("accounts", "devices"):
            result = admin.post(f"/admin/{args.command}/list", {})
        elif args.command == "revoke":
            result = admin.post("/admin/devices/revoke", {"device_id": args.device_id})
        else:
            result = admin.post("/admin/accounts/import", {"account_id": args.id,
                "credentials": private_json(args.credentials.expanduser()),
                "exclusive_owner": True, "replace_quarantined": args.replace_quarantined})
        print(json.dumps(result, indent=2))
        return 0
    except AdminError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        print("Administration failed; check private file permissions and configuration.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
