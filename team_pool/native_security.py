"""Bounded native credential access through Apple's stable security(1) identity.

The native item is the only item routed here. Secret writes go through stdin,
never process arguments, shell expansion, or temporary files. Background calls
first inspect the existing item's ACL without decrypting it. That is a preflight,
not an atomic UI guarantee: a concurrent lock or ACL change can still race it.
Such failures/timeouts pause the client instead of repeatedly requesting access.
"""

from __future__ import annotations

import ctypes
from contextlib import ExitStack
import json
import plistlib
import subprocess
import unicodedata

from .macos_keychain import KeychainWriteError


SERVICE = "Claude Code-credentials"
SECURITY = "/usr/bin/security"
MAX_COMMAND = 4032  # security(1)'s interactive input buffer is 4096 bytes.
MAX_READ = 4 * 1024 * 1024


class KeychainPayloadError(RuntimeError):
    """A native payload cannot be represented safely by the bounded writer."""


def _quoted(value):
    if not isinstance(value, str) or any(unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in value):
        raise KeychainPayloadError("Native Keychain command contains unsupported control characters.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_command(username, password):
    if not isinstance(password, str) or not password.isascii():
        raise KeychainPayloadError("Native credentials require ASCII-escaped JSON.")
    try:
        if not isinstance(json.loads(password), dict):
            raise ValueError
    except (ValueError, TypeError):
        raise KeychainPayloadError("Native credentials require a JSON object.") from None
    command = ("add-generic-password -U -a " + _quoted(username) + " -s " + _quoted(SERVICE)
               + " -w " + _quoted(password) + "\n").encode("utf-8")
    if len(command) > MAX_COMMAND:
        raise KeychainPayloadError("Native credentials exceed the safe security(1) input limit; no data was changed.")
    return command


class NativeSecurityCLI:
    def __init__(self, api, *, runner=subprocess.run, inspector=None):
        self.api = api
        self.runner = runner
        self.inspector = inspector

    def validate_password(self, username, password):
        write_command(username, password)

    def _preflight(self, username, background):
        _quoted(username)
        if not background:
            return
        try:
            inspector = self.inspector or NativeACLInspector(self.api)
            allowed = inspector.ready(SERVICE, username)
        except Exception:
            allowed = False
        if not allowed:
            raise KeychainWriteError(-25308)

    def _run(self, arguments, *, background, payload=None, reading=False):
        try:
            return self.runner(
                arguments, input=payload, stdin=subprocess.DEVNULL if payload is None else None,
                stdout=subprocess.PIPE if reading else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15 if background else 120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Exception fields can contain secret stdout or stdin. Drop all of it.
            raise KeychainWriteError(-25308) from None

    def get_password(self, username, *, background=False):
        self._preflight(username, background)
        result = self._run([SECURITY, "find-generic-password", "-a", username, "-s", SERVICE, "-w"],
                           background=background, reading=True)
        if result.returncode == 44 and not background:  # errSecItemNotFound
            return None
        if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > MAX_READ:
            raise KeychainWriteError(-25308)
        try:
            return result.stdout.removesuffix(b"\n").decode("utf-8")
        except UnicodeError:
            raise KeychainWriteError(-25308) from None

    def set_password(self, username, password, *, background=False):
        command = write_command(username, password)
        self._preflight(username, background)
        # Only one command: a later command could hide this command's exit status.
        result = self._run([SECURITY, "-i"], payload=command, background=background)
        if result.returncode != 0:
            raise KeychainWriteError(-25308)

    def delete_password(self, username, *, background=False):
        self._preflight(username, background)
        result = self._run([SECURITY, "delete-generic-password", "-a", username, "-s", SERVICE],
                           background=background)
        if result.returncode != 0:
            raise KeychainWriteError(-25308)


class NativeACLInspector:
    """Read only item references, ACLs, and lock state; never request secret data."""

    def __init__(self, api):
        self.api = api
        pointer = ctypes.c_void_p
        out = ctypes.POINTER(pointer)
        bind = api._bind
        security, cf = api._security, api._cf
        self.true_ref = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
        self.copy_item = bind(security, "SecItemCopyMatching", ctypes.c_int32, pointer, out)
        self.copy_access = bind(security, "SecKeychainItemCopyAccess", ctypes.c_int32, pointer, out)
        self.copy_keychain = bind(security, "SecKeychainItemCopyKeychain", ctypes.c_int32, pointer, out)
        self.keychain_status = bind(security, "SecKeychainGetStatus", ctypes.c_int32, pointer,
                                   ctypes.POINTER(ctypes.c_uint32))
        self.copy_acls = bind(security, "SecAccessCopyACLList", ctypes.c_int32, pointer, out)
        self.copy_tags = bind(security, "SecACLCopyAuthorizations", pointer, pointer)
        self.copy_contents = bind(security, "SecACLCopyContents", ctypes.c_int32, pointer,
                                 out, out, ctypes.POINTER(ctypes.c_uint16))
        self.copy_app_data = bind(security, "SecTrustedApplicationCopyData", ctypes.c_int32, pointer, out)
        self.validate_app = bind(security, "SecTrustedApplicationValidateWithPath", ctypes.c_int32,
                                 pointer, ctypes.c_char_p)
        self.array_count = bind(cf, "CFArrayGetCount", ctypes.c_long, pointer)
        self.array_item = bind(cf, "CFArrayGetValueAtIndex", pointer, pointer, ctypes.c_long)
        self.string_value = bind(cf, "CFStringGetCString", ctypes.c_ubyte, pointer, pointer,
                                 ctypes.c_long, ctypes.c_uint32)
        self.data_length = bind(cf, "CFDataGetLength", ctypes.c_long, pointer)
        self.data_bytes = bind(cf, "CFDataGetBytePtr", pointer, pointer)

    def _copy(self, stack, function, reference):
        result = ctypes.c_void_p()
        status = function(reference, ctypes.byref(result))
        if result.value:
            stack.enter_context(self.api._owned(result.value))
        if status != 0 or not result.value:
            raise ValueError("Keychain metadata unavailable")
        return result.value

    def _array(self, reference):
        if not reference:
            return []
        count = self.array_count(reference)
        if not 0 <= count <= 1024:
            raise ValueError("Keychain metadata exceeds supported limits")
        return [self.array_item(reference, i) for i in range(count)]

    def _string(self, reference):
        if not reference:
            raise ValueError("Missing Keychain ACL description")
        buffer = ctypes.create_string_buffer(65536)
        if not self.string_value(reference, buffer, len(buffer), 0x08000100):
            raise ValueError("Unsupported Keychain ACL description")
        return buffer.value.decode("utf-8")

    def _security_trusted(self, applications):
        for app in self._array(applications):
            with ExitStack() as stack:
                data = self._copy(stack, self.copy_app_data, app)
                length = self.data_length(data)
                if not 0 < length <= 4096:
                    continue
                path = ctypes.string_at(self.data_bytes(data), length).split(b"\0", 1)[0]
                if path == SECURITY.encode() and self.validate_app(app, SECURITY.encode()) == 0:
                    return True
        return False

    def ready(self, service, username):
        previous = self.api.get_user_interaction_allowed()
        self.api.set_user_interaction_allowed(False)
        try:
            with ExitStack() as stack:
                identity = self.api._identity(stack, service, username)
                query = stack.enter_context(self.api._dictionary(identity + [
                    (self.api._constant("kSecReturnRef"), self.true_ref),
                    (self.api._constant("kSecMatchLimit"), self.api._constant("kSecMatchLimitOne")),
                ]))
                item = self._copy(stack, self.copy_item, query)
                keychain = self._copy(stack, self.copy_keychain, item)
                status = ctypes.c_uint32()
                if self.keychain_status(keychain, ctypes.byref(status)) != 0 or not status.value & 1:
                    return False
                access = self._copy(stack, self.copy_access, item)
                acls = self._copy(stack, self.copy_acls, access)
                partitions = []
                decrypt_allowed = False
                for acl in self._array(acls):
                    with ExitStack() as acl_stack:
                        tags_ref = acl_stack.enter_context(self.api._owned(self.copy_tags(acl)))
                        tags = {self._string(value) for value in self._array(tags_ref)}
                        if not tags.intersection({"ACLAuthorizationPartitionID", "ACLAuthorizationDecrypt"}):
                            continue
                        apps, description, flags = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_uint16()
                        result = self.copy_contents(acl, ctypes.byref(apps), ctypes.byref(description), ctypes.byref(flags))
                        for value in (apps.value, description.value):
                            if value:
                                acl_stack.enter_context(self.api._owned(value))
                        if result != 0:
                            return False
                        if "ACLAuthorizationPartitionID" in tags:
                            value = plistlib.loads(bytes.fromhex(self._string(description.value)))
                            ids = value.get("Partitions") if isinstance(value, dict) else None
                            if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                                return False
                            partitions.append(ids)
                        if "ACLAuthorizationDecrypt" in tags and not flags.value & 1:
                            decrypt_allowed |= self._security_trusted(apps.value)
                return bool(partitions) and all("apple-tool:" in ids for ids in partitions) and decrypt_allowed
        except Exception:
            return False
        finally:
            self.api.set_user_interaction_allowed(previous)
