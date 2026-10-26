"""Update existing macOS Keychain items without replacing their ownership or ACL.

keyring 25.7's macOS setter deletes and re-adds an item. Claude's existing
credential item can reject that deletion even when updating its data is allowed.
Only an item-not-found result permits adding a new item here. All secret data
stays in process memory; the Security API never receives a shell command.
"""

from __future__ import annotations

import ctypes
from contextlib import ExitStack, contextmanager


ITEM_NOT_FOUND = -25300


class KeychainWriteError(RuntimeError):
    def __init__(self, status):
        self.status = int(status)
        super().__init__(f"Keychain write failed (OSStatus {self.status}).")


class MacOSKeychain:
    """Keyring-compatible adapter with an atomic, data-only setter."""

    def __init__(self, *, reader=None, api=None):
        if reader is None:
            from keyring.backends.macOS import Keyring
            reader = Keyring()
        self._reader = reader
        self._api = api if api is not None else _SecurityAPI()

    def get_password(self, service, username):
        return self._reader.get_password(service, username)

    def delete_password(self, service, username):
        # Explicit removal is used when leaving the pilot, never during set.
        return self._reader.delete_password(service, username)

    def set_password(self, service, username, password):
        username = "" if username is None else username
        data = password.encode("utf-8")
        status = self._api.update(service, username, data)
        if status == ITEM_NOT_FOUND:
            status = self._api.add(service, username, data)
        if status != 0:
            raise KeychainWriteError(status)


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
