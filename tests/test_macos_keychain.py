"""The setter tests never read or write a real Keychain item."""

import ctypes
import sys
import unittest

from team_pool.macos_keychain import (
    ITEM_NOT_FOUND, KeychainWriteError, MacOSKeychain, _SecurityAPI,
)


class FakeReader:
    def __init__(self):
        self.calls = []

    def get_password(self, service, username):
        self.calls.append(("get", service, username))
        return "fake-read"

    def delete_password(self, service, username):
        self.calls.append(("delete", service, username))

    def set_password(self, *args):
        raise AssertionError("The delete-and-add setter must never be used")


class FakeAPI:
    def __init__(self, *, update_status=0, add_status=0):
        self.update_status = update_status
        self.add_status = add_status
        self.calls = []

    def update(self, service, username, data):
        self.calls.append(("update", service, username, data))
        return self.update_status

    def add(self, service, username, data):
        self.calls.append(("add", service, username, data))
        return self.add_status


class MacOSKeychainTests(unittest.TestCase):
    def make_backend(self, **statuses):
        reader = FakeReader()
        api = FakeAPI(**statuses)
        return MacOSKeychain(reader=reader, api=api), reader, api

    def test_existing_item_updates_without_deleting_or_adding(self):
        backend, reader, api = self.make_backend()
        backend.set_password("service", "user", "fake-new")
        self.assertEqual(api.calls, [("update", "service", "user", b"fake-new")])
        self.assertEqual(reader.calls, [])

    def test_missing_item_is_added_only_after_not_found(self):
        backend, reader, api = self.make_backend(update_status=ITEM_NOT_FOUND)
        backend.set_password("service", "user", "fake-new")
        self.assertEqual(api.calls, [
            ("update", "service", "user", b"fake-new"),
            ("add", "service", "user", b"fake-new"),
        ])
        self.assertEqual(reader.calls, [])

    def test_update_denied_preserves_item_without_fallback(self):
        for status in (-25244, -128, -25293, -25308):
            with self.subTest(status=status):
                backend, reader, api = self.make_backend(update_status=status)
                with self.assertRaises(KeychainWriteError) as raised:
                    backend.set_password("sensitive-service", "sensitive-user", "sensitive-value")
                self.assertEqual(raised.exception.status, status)
                self.assertNotIn("sensitive", str(raised.exception))
                self.assertEqual([call[0] for call in api.calls], ["update"])
                self.assertEqual(reader.calls, [])

    def test_failed_add_does_not_delete_a_concurrently_created_item(self):
        backend, reader, api = self.make_backend(
            update_status=ITEM_NOT_FOUND, add_status=-25299)  # duplicate item
        with self.assertRaises(KeychainWriteError):
            backend.set_password("service", "user", "fake-new")
        self.assertEqual([call[0] for call in api.calls], ["update", "add"])
        self.assertEqual(reader.calls, [])

    def test_unicode_nuls_empty_and_large_values_are_complete_utf8_data(self):
        for value in ("", "秘密🌲\0fin", "🌲" * 300_000):
            with self.subTest(length=len(value)):
                backend, reader, api = self.make_backend()
                backend.set_password("共有", None, value)
                self.assertEqual(api.calls, [("update", "共有", "", value.encode("utf-8"))])
                self.assertEqual(reader.calls, [])

    def test_read_and_explicit_delete_delegate(self):
        backend, reader, api = self.make_backend()
        self.assertEqual(backend.get_password("service", "user"), "fake-read")
        backend.delete_password("service", "user")
        self.assertEqual(reader.calls, [("get", "service", "user"), ("delete", "service", "user")])
        self.assertEqual(api.calls, [])


class CFConstructionTests(unittest.TestCase):
    """Exercise query contents and CF ownership with fake framework functions."""

    def setUp(self):
        self.api = object.__new__(_SecurityAPI)
        self.values = {}
        self.next_ref = 100
        self.constants = {name: index for index, name in enumerate((
            "kSecClass", "kSecClassGenericPassword", "kSecAttrService",
            "kSecAttrAccount", "kSecValueData",
        ), start=1)}
        self.api._constant = self.constants.__getitem__
        self.api._keys = None
        self.api._values = None
        self.api._string = lambda _, data, length, encoding, external: self.allocate(data[:length].decode("utf-8"))
        self.api._data = lambda _, data, length: self.allocate(data[:length])
        self.api._dict = lambda _, keys, values, count, kc, vc: self.allocate({
            keys[i]: self.values.get(values[i], values[i]) for i in range(count)
        })
        self.api._release = self.release
        self.calls = []
        self.api._update = lambda query, changes: self.capture("update", query, changes)
        self.api._add = lambda query, result: self.capture("add", query)

    def allocate(self, value):
        self.next_ref += 1
        self.values[self.next_ref] = value
        return self.next_ref

    def release(self, reference):
        self.assertIn(reference, self.values, "Each created object must be released exactly once")
        del self.values[reference]

    def capture(self, kind, *references):
        self.calls.append((kind, *(self.values[ref] for ref in references)))
        return 0

    def test_update_query_changes_only_data_and_releases_every_object(self):
        self.assertEqual(self.api.update("共享\0", "人🌲", b"fake\0value"), 0)
        self.assertEqual(self.calls, [("update", {
            self.constants["kSecClass"]: self.constants["kSecClassGenericPassword"],
            self.constants["kSecAttrService"]: "共享\0",
            self.constants["kSecAttrAccount"]: "人🌲",
        }, {self.constants["kSecValueData"]: b"fake\0value"})])
        self.assertEqual(self.values, {})

    def test_add_query_includes_identity_and_data_then_releases_every_object(self):
        self.assertEqual(self.api.add("service", "user", b"fake"), 0)
        self.assertEqual(self.calls, [("add", {
            self.constants["kSecClass"]: self.constants["kSecClassGenericPassword"],
            self.constants["kSecAttrService"]: "service",
            self.constants["kSecAttrAccount"]: "user",
            self.constants["kSecValueData"]: b"fake",
        })])
        self.assertEqual(self.values, {})

    def test_framework_exception_releases_every_object(self):
        def fail(*args):
            raise RuntimeError("synthetic framework failure")
        self.api._update = fail
        with self.assertRaises(RuntimeError):
            self.api.update("service", "user", b"fake")
        self.assertEqual(self.values, {})

    def test_allocation_failure_releases_earlier_objects(self):
        self.api._data = lambda *args: None
        with self.assertRaises(MemoryError):
            self.api.update("service", "user", b"fake")
        self.assertEqual(self.values, {})

    @unittest.skipUnless(sys.platform == "darwin", "CoreFoundation is macOS-only")
    def test_real_cf_bindings_preserve_unicode_and_large_binary_data_without_keychain(self):
        api = _SecurityAPI()
        data_length = api._bind(api._cf, "CFDataGetLength", ctypes.c_long, ctypes.c_void_p)
        data_bytes = api._bind(api._cf, "CFDataGetBytePtr", ctypes.c_void_p, ctypes.c_void_p)
        string_length = api._bind(api._cf, "CFStringGetLength", ctypes.c_long, ctypes.c_void_p)
        dictionary_count = api._bind(api._cf, "CFDictionaryGetCount", ctypes.c_long, ctypes.c_void_p)
        dictionary_value = api._bind(api._cf, "CFDictionaryGetValue", ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_void_p)
        text = "秘密🌲\0"
        data = (text * 100_000).encode("utf-8")
        with api._cf_string(text) as reference:
            self.assertEqual(string_length(reference), len(text.encode("utf-16-le")) // 2)
        with api._cf_data(data) as reference:
            self.assertEqual(data_length(reference), len(data))
            self.assertEqual(ctypes.string_at(data_bytes(reference), len(data)), data)
            with api._dictionary([(api._constant("kSecValueData"), reference)]) as query:
                self.assertEqual(dictionary_count(query), 1)
                self.assertEqual(dictionary_value(query, api._constant("kSecValueData")), reference)


if __name__ == "__main__":
    unittest.main()
