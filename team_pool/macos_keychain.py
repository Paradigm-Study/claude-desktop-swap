"""Use the same Apple writer as Claude for its native credential item.

keyring 25.7's macOS setter deletes and re-adds an item. Claude's existing
credential item can reject that deletion even when updating its data is allowed.
Private pilot items use data-only Security API updates. The native item uses
security(1): a Python data write changes its partition ACL to Python's identity
and causes Claude's security(1) reader to request approval again.
"""

from __future__ import annotations

import ctypes
from contextlib import ExitStack, contextmanager


ITEM_NOT_FOUND = -25300
ACCESS_REQUIRED = frozenset({-25308, -128, -25293})


def access_required_status(error):
    """Read numeric OSStatus only, including keyring's chained API exception.

    Backend messages can contain secrets. Never parse or expose their text.
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        status = getattr(error, "status", None)
        if type(status) is int and status in ACCESS_REQUIRED:
            return status
        if error.args and type(error.args[0]) is int and error.args[0] in ACCESS_REQUIRED:
            return error.args[0]
        error = error.__cause__ or error.__context__
    return None


class KeychainWriteError(RuntimeError):
    def __init__(self, status):
        self.status = int(status)
        super().__init__(f"Keychain write failed (OSStatus {self.status}).")


class KeychainInteractionError(RuntimeError):
    """The process could not establish its required Keychain UI policy."""


class MacOSKeychain:
    """Keep native credentials on Apple's identity and pilot secrets on Python.

    Do not route the native item back through SecItemUpdate: that failed approach
    replaces its partition ACL with Python's identity even without deleting it.
    """

    def __init__(self, *, reader=None, api=None, native_cli=None):
        if reader is None:
            from keyring.backends.macOS import Keyring
            reader = Keyring()
        self._reader = reader
        self._api = api if api is not None else _SecurityAPI()
        from .native_security import NativeSecurityCLI
        self._native_cli = native_cli or NativeSecurityCLI(self._api)
        self._background = False

    def get_password(self, service, username):
        if service == "Claude Code-credentials":
            return self._native_cli.get_password(username, background=self._background)
        return self._reader.get_password(service, username)

    def delete_password(self, service, username):
        if service == "Claude Code-credentials":
            return self._native_cli.delete_password(username, background=self._background)
        # Explicit removal is used when leaving the pilot, never during set.
        return self._reader.delete_password(service, username)

    def set_password(self, service, username, password):
        if service == "Claude Code-credentials":
            return self._native_cli.set_password(username, password, background=self._background)
        username = "" if username is None else username
        data = password.encode("utf-8")
        status = self._api.update(service, username, data)
        if status == ITEM_NOT_FOUND:
            status = self._api.add(service, username, data)
        if status != 0:
            raise KeychainWriteError(status)

    def validate_password(self, service, username, password):
        if service == "Claude Code-credentials":
            self._native_cli.validate_password(username, password)

    @contextmanager
    def noninteractive(self):
        # Private pilot items use this process-wide flag. The native security(1)
        # child does not inherit it and gets a separate metadata/ACL preflight.
        previous = self._api.get_user_interaction_allowed()
        self._api.set_user_interaction_allowed(False)
        previous_background = self._background
        self._background = True
        try:
            yield
        finally:
            self._background = previous_background
            self._api.set_user_interaction_allowed(previous)


class _SecurityAPI:
    """Small lazy ctypes binding. Every created CF object has a scoped release."""

    def __init__(self):
        self._security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security")
        self._cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer = ctypes.c_void_p
        index = ctypes.c_long  # CFIndex is a signed pointer-sized integer.
        self._update = self._bind(self._security, "SecItemUpdate", ctypes.c_int32,
                                  pointer, pointer)
        self._add = self._bind(self._security, "SecItemAdd", ctypes.c_int32,
                               pointer, pointer)
        self._get_interaction = self._bind(
            self._security, "SecKeychainGetUserInteractionAllowed", ctypes.c_int32,
            ctypes.POINTER(ctypes.c_ubyte))
        self._set_interaction = self._bind(
            self._security, "SecKeychainSetUserInteractionAllowed", ctypes.c_int32,
            ctypes.c_ubyte)
        self._string = self._bind(self._cf, "CFStringCreateWithBytes", pointer,
                                  pointer, pointer, index, ctypes.c_uint32,
                                  ctypes.c_ubyte)
        self._data = self._bind(self._cf, "CFDataCreate", pointer,
                                pointer, pointer, index)
        self._dict = self._bind(self._cf, "CFDictionaryCreate", pointer,
                                pointer, pointer, pointer, index, pointer, pointer)
        self._release = self._bind(self._cf, "CFRelease", None, pointer)
        self._keys = ctypes.byref(ctypes.c_byte.in_dll(
            self._cf, "kCFTypeDictionaryKeyCallBacks"))
        self._values = ctypes.byref(ctypes.c_byte.in_dll(
            self._cf, "kCFTypeDictionaryValueCallBacks"))

    @staticmethod
    def _bind(library, name, result, *arguments):
        function = getattr(library, name)
        function.restype = result
        function.argtypes = arguments
        return function

    def _constant(self, name):
        # Exported constants belong to the framework and must not be released.
        return ctypes.c_void_p.in_dll(self._security, name).value

    def get_user_interaction_allowed(self):
        allowed = ctypes.c_ubyte()
        if self._get_interaction(ctypes.byref(allowed)) != 0:
            raise KeychainInteractionError("Could not read the process Keychain interaction policy.")
        return bool(allowed.value)

    def set_user_interaction_allowed(self, allowed):
        if self._set_interaction(bool(allowed)) != 0:
            raise KeychainInteractionError("Could not set the process Keychain interaction policy.")

    @contextmanager
    def _owned(self, reference):
        if not reference:
            raise MemoryError("Could not allocate a Keychain query.")
        try:
            yield reference
        finally:
            self._release(reference)

    def _cf_string(self, text):
        data = text.encode("utf-8")
        # An explicit length preserves Unicode and embedded NULs.
        return self._owned(self._string(None, data, len(data), 0x08000100, False))

    def _cf_data(self, data):
        return self._owned(self._data(None, data, len(data)))

    def _dictionary(self, pairs):
        count = len(pairs)
        keys = (ctypes.c_void_p * count)(*(key for key, _ in pairs))
        values = (ctypes.c_void_p * count)(*(value for _, value in pairs))
        return self._owned(self._dict(None, keys, values, count, self._keys, self._values))

    def _identity(self, stack, service, username):
        service_ref = stack.enter_context(self._cf_string(service))
        username_ref = stack.enter_context(self._cf_string(username))
        return [
            (self._constant("kSecClass"), self._constant("kSecClassGenericPassword")),
            (self._constant("kSecAttrService"), service_ref),
            (self._constant("kSecAttrAccount"), username_ref),
        ]

    def update(self, service, username, data):
        with ExitStack() as stack:
            identity = self._identity(stack, service, username)
            query = stack.enter_context(self._dictionary(identity))
            data_ref = stack.enter_context(self._cf_data(data))
            changes = stack.enter_context(self._dictionary([
                (self._constant("kSecValueData"), data_ref),
            ]))
            return int(self._update(query, changes))

    def add(self, service, username, data):
        with ExitStack() as stack:
            identity = self._identity(stack, service, username)
            data_ref = stack.enter_context(self._cf_data(data))
            query = stack.enter_context(self._dictionary(identity + [
                (self._constant("kSecValueData"), data_ref),
            ]))
            return int(self._add(query, None))
