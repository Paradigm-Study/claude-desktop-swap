"""Single-writer, encrypted native-subscription credential coordinator pilot."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.fernet import Fernet
from .provider import Provider, ProviderError

FRESH_SECONDS = 300
POLL_SECONDS = 120
EXPIRY_MARGIN = 60
REFRESH_MARGIN = 300
THRESHOLD = 90
DEVICE_ACTIVE_SECONDS = 300
MAX_BODY = 8192
CONTRIBUTION_TTL = 600
CONTRIBUTION_INVALID_ATTEMPTS = 3
CONTRIBUTION_STARTS_PER_HOUR = 5
CONTRIBUTION_STARTS_PER_DAY = 20
CONTRIBUTION_CODE_LIMIT = 4225
METADATA = ("accountUuid", "emailAddress", "organizationUuid", "subscriptionType", "rateLimitTier")


class PoolError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private_file(path, *, check_only=False):
    """Read a user-owned regular file through its checked descriptor."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PoolError("private-file-must-be-owned-mode-0600")
        if check_only:
            return None
        data = stream.read(1_048_577)
        if len(data) > 1_048_576:
            raise PoolError("private-file-too-large")
        return data


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def validate_credentials(value):
    if not isinstance(value, dict) or set(value) != {"access_token", "refresh_token", "expires_at", "scopes", "account"}:
        raise PoolError("invalid-credential-schema")
    for field in ("access_token", "refresh_token"):
        if not isinstance(value[field], str) or not 1 <= len(value[field]) <= 16384 or any(c.isspace() for c in value[field]):
            raise PoolError("invalid-credential-schema")
    expiry = value["expires_at"]
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= 0:
        raise PoolError("invalid-credential-expiry")
    scopes = value["scopes"]
    if not isinstance(scopes, list) or "user:inference" not in scopes or any(not isinstance(s, str) or not s or len(s) > 256 for s in scopes):
        raise PoolError("invalid-credential-scopes")
    account = value["account"]
    if not isinstance(account, dict) or set(account) != set(METADATA):
        raise PoolError("invalid-account-metadata")
    if any(not isinstance(account[key], str) or not account[key] or len(account[key]) > 512 for key in METADATA):
        raise PoolError("invalid-account-metadata")
    return value


class Coordinator:
    def __init__(self, state, *, provider=None, clock=time.time, create=False):
        self.path, self.clock = Path(state).expanduser(), clock
        self.provider = provider or Provider(clock=clock)
        self.lock = threading.RLock()
        self.db = self.lockfile = None
        if create:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PoolError("state-directory-must-be-owned-mode-0700")
        try:
            lock_path = self.path / "writer.lock"
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self.lockfile = os.fdopen(fd, "r+")
            file_info = os.fstat(fd)
            if file_info.st_uid != os.getuid() or stat.S_IMODE(file_info.st_mode) != 0o600:
                raise PoolError("unsafe-writer-lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PoolError("coordinator-already-running-stop-before-admin-changes", 409) from None
            key = self.path / "master.key"
            database = self.path / "pool.sqlite3"
            if create and not key.exists() and not database.exists():
                write_private(key, Fernet.generate_key())
            self.cipher = Fernet(private_file(key))
            if database.exists() or database.is_symlink():
                private_file(database, check_only=True)
            else:
                write_private(database, b"")
            self.db = sqlite3.connect(database, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                  id TEXT PRIMARY KEY, secret BLOB NOT NULL, generation INTEGER NOT NULL,
                  state TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
                  usage TEXT, fetched REAL, next_poll REAL NOT NULL DEFAULT 0,
                  failures INTEGER NOT NULL DEFAULT 0, error TEXT);
                CREATE TABLE IF NOT EXISTS invitations (
                  token_hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS devices (
                  id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                  revoked INTEGER NOT NULL DEFAULT 0, account_id TEXT REFERENCES accounts(id),
                  last_seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS contributions (
                  id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
                  created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
                  secret BLOB, invalid_attempts INTEGER NOT NULL DEFAULT 0,
                  account_id TEXT REFERENCES accounts(id));
                CREATE INDEX IF NOT EXISTS contribution_device_created
                  ON contributions(device_id, created);
                CREATE TABLE IF NOT EXISTS audit (
                  at REAL NOT NULL, event TEXT NOT NULL, subject TEXT NOT NULL);
            ''')
            with self.db:
                self.db.execute("UPDATE accounts SET state='quarantined',error='interrupted-refresh' WHERE state='refreshing'")
                # A grant may already have been consumed. Never replay one after
                # an interrupted exchange, including after a successful response
                # that was not committed to the account table.
                self.db.execute("UPDATE contributions SET state='uncertain',secret=NULL WHERE state='exchanging'")
                self.db.execute("UPDATE contributions SET state='expired',secret=NULL WHERE state='pending' AND expires<=?", (self.clock(),))
        except Exception:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lockfile is not None:
            self.lockfile.close()
            self.lockfile = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _audit(self, event, subject):
        self.db.execute("INSERT INTO audit VALUES (?,?,?)", (self.clock(), event, subject))

    def add_account(self, account_id, credentials, *, exclusive_owner=False, replace_quarantined=False):
        if not exclusive_owner:
            raise PoolError("exclusive-refresh-ownership-required")
        if not isinstance(account_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", account_id):
            raise PoolError("invalid-account-id")
        validate_credentials(credentials)
        secret = self.cipher.encrypt(json.dumps(credentials).encode())
        with self.lock, self.db:
            existing = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if existing is not None:
                if not replace_quarantined or existing["state"] != "quarantined":
                    raise PoolError("account-already-exists", 409)
                previous = json.loads(self.cipher.decrypt(existing["secret"]))
                if previous["account"]["accountUuid"] != credentials["account"]["accountUuid"]:
                    raise PoolError("replacement-account-identity-mismatch", 409)
                if previous["refresh_token"] == credentials["refresh_token"]:
                    raise PoolError("replacement-requires-fresh-credential-lineage", 409)
                self.db.execute("UPDATE accounts SET secret=?,generation=generation+1,state='ready',verified=0,usage=NULL,fetched=NULL,next_poll=0,failures=0,error=NULL WHERE id=?", (secret, account_id))
                self._audit("quarantined-account-replaced", account_id)
                return
            if replace_quarantined:
                raise PoolError("unknown-quarantined-account", 404)
            # One logical provider account per pool. Do not disguise duplicate
            # credential lineages as independent quota capacity.
            for row in self.db.execute("SELECT secret FROM accounts"):
                prior = json.loads(self.cipher.decrypt(row[0]))
                if prior["account"]["accountUuid"] == credentials["account"]["accountUuid"]:
                    raise PoolError("provider-account-already-exists", 409)
            self.db.execute("INSERT INTO accounts(id,secret,generation,state) VALUES (?,?,1,'ready')", (account_id, secret))
            self._audit("account-added", account_id)

    def invite(self, ttl=600):
        if not 30 <= ttl <= 86400:
            raise PoolError("invitation-ttl-out-of-range")
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute("INSERT INTO invitations VALUES (?,?,0)", (digest(token), self.clock() + ttl))
            self._audit("invitation-created", "")
        return token

    def enroll(self, invitation, device_name):
        if not isinstance(invitation, str) or not 32 <= len(invitation) <= 256:
            raise PoolError("invalid-invitation", 401)
        if not isinstance(device_name, str) or not 1 <= len(device_name) <= 80 or any(ord(c) < 32 for c in device_name):
            raise PoolError("invalid-device-name")
        with self.lock, self.db:
            changed = self.db.execute("UPDATE invitations SET used=1 WHERE token_hash=? AND used=0 AND expires>?", (digest(invitation), self.clock())).rowcount
            if changed != 1:
                raise PoolError("invalid-invitation", 401)
            device, token = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO devices(id,token_hash,name,last_seen) VALUES (?,?,?,?)", (device, digest(token), device_name, self.clock()))
            self._audit("device-enrolled", device)
            return {"device_id": device, "device_token": token}

    def devices(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT id,name,revoked,account_id,last_seen FROM devices ORDER BY last_seen DESC")]

    def accounts(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT id,generation,state,verified,fetched,next_poll,error FROM accounts ORDER BY id")]

    def revoke(self, device_id):
        with self.lock, self.db:
            if not self.db.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,)).rowcount:
                raise PoolError("unknown-device", 404)
            self._audit("device-revoked", device_id)

    def authenticate_device(self, token):
        """Authenticate without contacting the provider or reading a request body."""
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise PoolError("unauthorized", 401)
        with self.lock:
            device = self.db.execute("SELECT * FROM devices WHERE token_hash=? AND revoked=0", (digest(token),)).fetchone()
            if device is None:
                raise PoolError("unauthorized", 401)
            return device

    def _expire_contributions(self):
        with self.db:
            self.db.execute("UPDATE contributions SET state='expired',secret=NULL WHERE state='pending' AND expires<=?", (self.clock(),))

    def _contribution(self, device_id, contribution_id):
        if not isinstance(contribution_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", contribution_id):
            raise PoolError("unknown-contribution", 404)
        row = self.db.execute("SELECT * FROM contributions WHERE id=? AND device_id=?", (contribution_id, device_id)).fetchone()
        if row is None:
            raise PoolError("unknown-contribution", 404)
        return row

    @staticmethod
    def _contribution_receipt(row):
        result = {"contribution_id": row["id"], "state": row["state"], "expires_at": row["expires"]}
        if row["account_id"] is not None:
            result["account_id"] = row["account_id"]
        return result

    def _finish_contribution(self, row, state, account_id=None):
        # Caller owns the transaction so an account and its receipt are atomic.
        self.db.execute("UPDATE contributions SET state=?,secret=NULL,account_id=? WHERE id=?", (state, account_id, row["id"]))
        self._audit("contribution-" + state, row["id"])
        return self._contribution_receipt(self._contribution(row["device_id"], row["id"]))

    def contribution_start(self, token):
        with self.lock:
            device = self.authenticate_device(token)
            self._expire_contributions()
            row = self.db.execute("SELECT * FROM contributions WHERE device_id=? AND state='pending'", (device["id"],)).fetchone()
            if row is None:
                now = self.clock()
                counts = self.db.execute("SELECT sum(created>?),count(*) FROM contributions WHERE device_id=? AND created>?",
                                         (now - 3600, device["id"], now - 86400)).fetchone()
                if (counts[0] or 0) >= CONTRIBUTION_STARTS_PER_HOUR or counts[1] >= CONTRIBUTION_STARTS_PER_DAY:
                    raise PoolError("contribution-start-limit", 429)
                contribution_id = secrets.token_urlsafe(24)
                secret = {"verifier": secrets.token_urlsafe(48), "state": secrets.token_urlsafe(32)}
                encrypted = self.cipher.encrypt(json.dumps(secret).encode())
                # Generate/validate before persisting. This call must be local;
                # provider traffic begins only after an authorization code.
                authorization_url = self.provider.authorization_url(secret["verifier"], secret["state"])
                with self.db:
                    self.db.execute("INSERT INTO contributions(id,device_id,created,expires,state,secret) VALUES (?,?,?,?,'pending',?)",
                                    (contribution_id, device["id"], now, now + CONTRIBUTION_TTL, encrypted))
                    self._audit("contribution-started", contribution_id)
                row = self._contribution(device["id"], contribution_id)
            else:
                secret = json.loads(self.cipher.decrypt(row["secret"]))
                authorization_url = self.provider.authorization_url(secret["verifier"], secret["state"])
            return {**self._contribution_receipt(row), "authorization_url": authorization_url}

    def contribution_status(self, token, contribution_id):
        with self.lock:
            device = self.authenticate_device(token)
            self._expire_contributions()
            return self._contribution_receipt(self._contribution(device["id"], contribution_id))

    def contribution_cancel(self, token, contribution_id):
        with self.lock:
            device = self.authenticate_device(token)
            self._expire_contributions()
            row = self._contribution(device["id"], contribution_id)
            if row["state"] == "pending":
                with self.db:
                    return self._finish_contribution(row, "cancelled")
            return self._contribution_receipt(row)

    def contribution_complete(self, token, contribution_id, code):
        # The single-writer lock covers this exchange just like native refresh.
        # It also makes duplicate concurrent completions return one receipt.
        with self.lock:
            device = self.authenticate_device(token)
            self._expire_contributions()
            row = self._contribution(device["id"], contribution_id)
            if row["state"] != "pending":
                return self._contribution_receipt(row)
            secret = json.loads(self.cipher.decrypt(row["secret"]))
            valid = isinstance(code, str) and len(code) <= CONTRIBUTION_CODE_LIMIT and code.count("#") == 1
            raw_code, callback_state = code.split("#", 1) if valid else ("", "")
            valid = (valid and 1 <= len(raw_code) <= 4096 and all(33 <= ord(char) <= 126 for char in raw_code)
                     and bool(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", callback_state))
                     and secrets.compare_digest(callback_state, secret["state"]))
            if not valid:
                with self.db:
                    attempts = row["invalid_attempts"] + 1
                    self.db.execute("UPDATE contributions SET invalid_attempts=? WHERE id=?", (attempts, row["id"]))
                    if attempts >= CONTRIBUTION_INVALID_ATTEMPTS:
                        return self._finish_contribution(row, "failed")
                raise PoolError("invalid-authorization-code")
            # Commit before the first external call. Do not persist the code.
            with self.db:
                self.db.execute("UPDATE contributions SET state='exchanging' WHERE id=?", (row["id"],))
                self._audit("contribution-exchanging", row["id"])
            try:
                credentials = validate_credentials(self.provider.exchange_code(raw_code, secret["verifier"], secret["state"]))
                if credentials["expires_at"] <= self.clock() + EXPIRY_MARGIN or credentials["account"]["subscriptionType"] not in {"pro", "max", "team", "enterprise"}:
                    raise PoolError("ineligible-subscription")
                # exchange_code verifies profile metadata against the grant and
                # requires a paid native plan. Never trust client-supplied IDs.
                with self.db:
                    for account in self.db.execute("SELECT id,secret FROM accounts"):
                        prior = json.loads(self.cipher.decrypt(account["secret"]))
                        if prior["account"]["accountUuid"] == credentials["account"]["accountUuid"]:
                            return self._finish_contribution(row, "duplicate", account["id"])
                    account_id = "contrib-" + secrets.token_hex(16)
                    encrypted = self.cipher.encrypt(json.dumps(credentials).encode())
                    self.db.execute("INSERT INTO accounts(id,secret,generation,state,verified) VALUES (?,?,1,'ready',1)", (account_id, encrypted))
                    self._audit("account-added", account_id)
                    return self._finish_contribution(row, "added", account_id)
            except Exception:
                # Provider response bodies/errors can carry credential values.
                # Even validation failure follows a possibly consumed code, so
                # preserve no grant material and never retry it automatically.
                with self.db:
                    return self._finish_contribution(row, "uncertain")

    def _quarantine(self, account_id, error):
        with self.db:
            self.db.execute("UPDATE accounts SET state='quarantined',error=? WHERE id=?", (error, account_id))
            self._audit("account-quarantined", account_id)

    def poll(self):
        """Serialize every grant; commit in-flight BEFORE touching the network."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM accounts WHERE state='ready' ORDER BY id").fetchall()
            for row in rows:
                now = self.clock()
                credentials = json.loads(self.cipher.decrypt(row["secret"]))
                if credentials["expires_at"] <= now + REFRESH_MARGIN:
                    with self.db:
                        self.db.execute("UPDATE accounts SET state='refreshing' WHERE id=?", (row["id"],))
                        self._audit("refresh-started", row["id"])
                    try:
                        successor = validate_credentials(self.provider.refresh(credentials))
                        encrypted = self.cipher.encrypt(json.dumps(successor).encode())
                        with self.db:
                            self.db.execute("UPDATE accounts SET secret=?,generation=generation+1,state='ready',verified=0,next_poll=0 WHERE id=?", (encrypted, row["id"]))
                            self._audit("refresh-completed", row["id"])
                        credentials = successor
                        row = self.db.execute("SELECT * FROM accounts WHERE id=?", (row["id"],)).fetchone()
                    except Exception:
                        self._quarantine(row["id"], "refresh-uncertain-reimport-fresh-lineage")
                        continue
                if now < row["next_poll"]:
                    continue
                try:
                    if not row["verified"]:
                        identity = self.provider.identity(credentials["access_token"])
                        if any(identity.get(key) != credentials["account"][key] for key in ("accountUuid", "organizationUuid")):
                            self._quarantine(row["id"], "identity-mismatch")
                            continue
                        if identity.get("emailAddress"):
                            credentials["account"]["emailAddress"] = identity["emailAddress"]
                        with self.db:
                            self.db.execute("UPDATE accounts SET verified=1,secret=? WHERE id=?", (self.cipher.encrypt(json.dumps(credentials).encode()), row["id"]))
                    usage = self.provider.usage(credentials["access_token"])
                    # Provider adapters/fakes must obey the same fail-closed contract.
                    if not isinstance(usage, dict) or not {"five_hour", "seven_day"} <= set(usage) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in usage.values()):
                        raise ProviderError("bad-usage")
                    with self.db:
                        self.db.execute("UPDATE accounts SET usage=?,fetched=?,next_poll=?,failures=0,error=NULL WHERE id=?", (json.dumps(usage), self.clock(), self.clock() + POLL_SECONDS, row["id"]))
                except Exception as exc:
                    # Never persist exception messages: upstream bodies can contain secrets.
                    code = exc.code if isinstance(exc, ProviderError) else "provider-error"
                    if code not in {"http-429", "http-401", "http-403", "bad-usage", "bad-profile", "transport-or-response-error"}:
                        code = "provider-error"
                    if code in {"http-401", "http-403"}:
                        self._quarantine(row["id"], code)
                        continue
                    failures = row["failures"] + 1
                    base = 600 if code == "http-429" else 120
                    delay = min(3600, base * 2 ** min(failures - 1, 5))
                    retry = exc.retry_after if isinstance(exc, ProviderError) else 0
                    with self.db:
                        self.db.execute("UPDATE accounts SET next_poll=?,failures=?,error=? WHERE id=?", (self.clock() + max(delay, retry), failures, code, row["id"]))

    def assignment(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise PoolError("unauthorized", 401)
        with self.lock:
            device = self.db.execute("SELECT * FROM devices WHERE token_hash=? AND revoked=0", (digest(token),)).fetchone()
            if device is None:
                raise PoolError("unauthorized", 401)
            self.poll()
            now = self.clock()
            rows = self.db.execute("SELECT * FROM accounts WHERE state='ready' AND verified=1").fetchall()
            usable, eligible = {}, []
            loads = {row["account_id"]: row["n"] for row in self.db.execute("SELECT account_id,count(*) n FROM devices WHERE revoked=0 AND last_seen>? GROUP BY account_id", (now - DEVICE_ACTIVE_SECONDS,))}
            for row in rows:
                credentials = json.loads(self.cipher.decrypt(row["secret"]))
                if credentials["expires_at"] <= now + EXPIRY_MARGIN:
                    continue
                usable[row["id"]] = (row, credentials)
                if row["fetched"] is not None and 0 <= now - row["fetched"] <= FRESH_SECONDS:
                    usage = json.loads(row["usage"])
                    if all(value < THRESHOLD for value in usage.values()):
                        eligible.append((loads.get(row["id"], 0), max(usage.values()), row["id"]))
            current = device["account_id"]
            candidates = {item[2] for item in eligible}
            if current in candidates:
                chosen, state = current, "fresh"
            elif eligible:
                chosen, state = min(eligible)[2], "fresh"
            elif current in usable:
                chosen = current
                row = usable[current][0]
                state = "quota_stale" if row["fetched"] is None or now - row["fetched"] > FRESH_SECONDS else "quota_exhausted"
            else:
                raise PoolError("no-eligible-account", 503)
            row, credentials = usable[chosen]
            with self.db:
                self.db.execute("UPDATE devices SET account_id=?,last_seen=? WHERE id=?", (chosen, now, device["id"]))
                if current != chosen:
                    self._audit("assignment-changed", device["id"])
            return {"account_id": chosen, "generation": row["generation"], "access_token": credentials["access_token"],
                    "expires_at": credentials["expires_at"], "scopes": credentials["scopes"],
                    "account": {key: credentials["account"][key] for key in METADATA},
                    "renew_after": min(60, max(1, credentials["expires_at"] - now - EXPIRY_MARGIN)), "state": state}


class PoolHTTPServer(ThreadingHTTPServer):
    # Wait for active credential operations before closing the coordinator DB.
    # Socket/provider timeouts bound individual network operations.
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, coordinator):
        self.coordinator = coordinator
        super().__init__(address, PoolHandler)

    def handle_error(self, request, client_address):
        # The default traceback can contain credential-bearing objects.
        pass


class PoolHandler(BaseHTTPRequestHandler):
    server_version = "NativePoolPilot/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def _reply(self, status, value):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        try:
            contribution_routes = {
                "/v1/contributions/start", "/v1/contributions/complete",
                "/v1/contributions/status", "/v1/contributions/cancel",
            }
            if self.path not in {"/v1/enroll", "/v1/assignment"} | contribution_routes:
                raise PoolError("not-found", 404)
            token = None
            if self.path in contribution_routes:
                auth = self.headers.get_all("Authorization", [])
                if len(auth) != 1 or not auth[0].startswith("Bearer "):
                    raise PoolError("unauthorized", 401)
                token = auth[0][7:]
                # Reject revoked/unknown devices before parsing any contribution
                # body, doing cryptography, or making a provider request.
                self.server.coordinator.authenticate_device(token)
            if self.headers.get("Transfer-Encoding"):
                raise PoolError("unsupported-transfer-encoding")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise PoolError("invalid-body-size", 413)
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                raise PoolError("invalid-body-size", 413)
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise PoolError("invalid-body")
                    value[key] = item
                return value
            body = json.loads(self.rfile.read(length), object_pairs_hook=unique_object)
            if not isinstance(body, dict):
                raise PoolError("invalid-body")
            if self.path == "/v1/enroll":
                if set(body) != {"invitation", "device_name"}:
                    raise PoolError("invalid-body")
                result = self.server.coordinator.enroll(body["invitation"], body["device_name"])
            elif self.path == "/v1/assignment":
                if body:
                    raise PoolError("invalid-body")
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    raise PoolError("unauthorized", 401)
                result = self.server.coordinator.assignment(auth[7:])
            elif self.path == "/v1/contributions/start":
                if body:
                    raise PoolError("invalid-body")
                result = self.server.coordinator.contribution_start(token)
            elif self.path == "/v1/contributions/complete":
                if set(body) != {"contribution_id", "code"}:
                    raise PoolError("invalid-body")
                result = self.server.coordinator.contribution_complete(token, body["contribution_id"], body["code"])
            else:
                if set(body) != {"contribution_id"}:
                    raise PoolError("invalid-body")
                method = (self.server.coordinator.contribution_cancel if self.path.endswith("/cancel")
                          else self.server.coordinator.contribution_status)
                result = method(token, body["contribution_id"])
            self._reply(200, result)
        except PoolError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, TypeError):
            self._reply(400, {"error": "invalid-body"})
        except Exception:
            self._reply(500, {"error": "internal-error"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="~/.local/share/claude-desktop-team-pool")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    add = commands.add_parser("add-account")
    add.add_argument("--id", required=True)
    add.add_argument("--credentials", required=True)
    add.add_argument("--exclusive-owner", action="store_true")
    add.add_argument("--replace-quarantined", action="store_true")
    invite = commands.add_parser("invite")
    invite.add_argument("--out", required=True)
    invite.add_argument("--ttl", type=int, default=600)
    commands.add_parser("devices")
    commands.add_parser("accounts")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("device_id")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8741)
    serve.add_argument("--tls-cert")
    serve.add_argument("--tls-key")
    args = parser.parse_args(argv)
    try:
        with Coordinator(args.state, create=args.command == "init") as coordinator:
            if args.command == "init":
                print("Coordinator state initialized (no accounts imported).")
            elif args.command == "add-account":
                coordinator.add_account(args.id, json.loads(private_file(Path(args.credentials).expanduser())), exclusive_owner=args.exclusive_owner, replace_quarantined=args.replace_quarantined)
                print("Account imported; identity and quota will be checked before delivery.")
            elif args.command == "invite":
                # Exclusive creation avoids overwriting a private file accidentally.
                write_private(Path(args.out).expanduser(), (coordinator.invite(args.ttl) + "\n").encode())
                print("Single-use invitation written to the private output file.")
            elif args.command == "devices":
                print(json.dumps(coordinator.devices(), indent=2))
            elif args.command == "accounts":
                print(json.dumps(coordinator.accounts(), indent=2))
            elif args.command == "revoke":
                coordinator.revoke(args.device_id)
                print("Device revoked. Previously delivered provider tokens may remain valid until expiry.")
            elif args.command == "serve":
                try:
                    loopback = ipaddress.ip_address(args.host).is_loopback
                except ValueError:
                    loopback = args.host == "localhost"
                if bool(args.tls_cert) != bool(args.tls_key) or (not loopback and not args.tls_cert):
                    raise PoolError("non-loopback-listener-requires-tls-cert-and-key")
                server = PoolHTTPServer((args.host, args.port), coordinator)
                if args.tls_cert:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.minimum_version = ssl.TLSVersion.TLSv1_2
                    context.load_cert_chain(args.tls_cert, args.tls_key)
                    server.socket = context.wrap_socket(server.socket, server_side=True)
                print("Coordinator listening. Stop it before running local administration commands.", flush=True)
                try:
                    server.serve_forever(poll_interval=0.5)
                finally:
                    server.shutdown()
                    server.server_close()
        return 0
    except KeyboardInterrupt:
        return 0
    except PoolError as exc:
        print("Error: " + exc.code, file=sys.stderr)
        return 1
    except Exception:
        print("Error: operation-failed (check private file permissions, state, and TLS configuration)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
