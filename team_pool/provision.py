"""Explicitly prepare ONE broker-owned pilot account for local import.

Run after native Claude Code login in a dedicated provisioning OS account.
Stop its Claude/cswap processes before export; never resume a second refresh
writer for this credential lineage. No source is selected automatically.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import sys

from .server import PoolError, private_file, validate_credentials, write_private


def normalize(credentials, config):
    try:
        oauth = credentials["claudeAiOauth"]
        account = config["oauthAccount"]
        if isinstance(oauth["expiresAt"], bool) or not isinstance(oauth["expiresAt"], (int, float)):
            raise ValueError
        value = {
            "access_token": oauth["accessToken"],
            "refresh_token": oauth["refreshToken"],
            "expires_at": oauth["expiresAt"] / 1000,
            "scopes": oauth["scopes"],
            "account": {key: account[key] for key in
                        ("accountUuid", "emailAddress", "organizationUuid")},
        }
        for key in ("subscriptionType", "rateLimitTier"):
            value["account"][key] = oauth[key]
        if not isinstance(value["scopes"], list) or "user:inference" not in value["scopes"]:
            raise ValueError
        return validate_credentials(value)
    except (KeyError, TypeError, ValueError, PoolError):
        raise PoolError("incomplete-native-subscription-credentials-or-metadata") from None


def macos_source():
    if sys.platform != "darwin":
        raise PoolError("macos-keychain-source-requires-macos")
    if any(os.environ.get(key) for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SECURE_STORE_DIR", "CLAUDE_CODE_PROFILE")):
        raise PoolError("nondefault-native-store-is-not-supported")
    from keyring.backends.macOS import Keyring
    raw = Keyring().get_password("Claude Code-credentials", pwd.getpwuid(os.getuid()).pw_name)
    if not raw:
        raise PoolError("native-subscription-credential-not-found")
    config = json.loads(private_file(Path.home() / ".claude.json"))
    return json.loads(raw), config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-macos-keychain", action="store_true")
    source.add_argument("--native-credentials", type=Path)
    parser.add_argument("--native-config", type=Path,
                        help="Required with --native-credentials; explicit Claude global JSON config")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--exclusive-owner", action="store_true", required=True,
                        help="Confirm all other refresh writers for this broker-owned lineage are stopped")
    args = parser.parse_args(argv)
    try:
        if args.from_macos_keychain:
            if args.native_config:
                raise PoolError("native-config-is-only-for-file-source")
            credentials, config = macos_source()
        else:
            if not args.native_config:
                raise PoolError("file-source-requires-native-config")
            credentials = json.loads(private_file(args.native_credentials.expanduser()))
            config = json.loads(private_file(args.native_config.expanduser()))
        result = normalize(credentials, config)
        write_private(args.out.expanduser(), (json.dumps(result) + "\n").encode())
        print("Private import file created. Keep every other refresh writer for this lineage stopped.")
        print("Import it locally into the coordinator, then remove this plaintext transfer file.")
        return 0
    except PoolError as exc:
        print("Account preparation refused: " + exc.code, file=sys.stderr)
    except Exception:
        print("Account preparation failed; check selected source, Keychain access, and private file permissions.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
