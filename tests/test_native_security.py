"""Apple CLI and ACL guards with synthetic credentials and mocked frameworks only."""

from contextlib import contextmanager
import ctypes
import json
import plistlib
import shlex
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from team_pool.macos_keychain import KeychainWriteError, MacOSKeychain
from team_pool.native_security import (
    MAX_COMMAND, SECURITY, SERVICE, KeychainPayloadError, NativeACLInspector,
    NativeSecurityCLI, write_command,
)


class SecurityCLITests(unittest.TestCase):
    def setUp(self):
        self.runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"fake":"credential"}\n'))
        self.inspector = Mock()
        self.inspector.ready.return_value = True
        self.cli = NativeSecurityCLI(None, runner=self.runner, inspector=self.inspector)

    def test_write_uses_only_fixed_process_arguments_and_one_escaped_stdin_command(self):
        payload = json.dumps({"fake": 'quote" slash\\; $HOME $(false) `false` \n秘密'}, ensure_ascii=True)
        self.cli.set_password("test-user", payload, background=True)
        arguments, options = self.runner.call_args
        self.assertEqual(arguments, ([SECURITY, "-i"],))
        self.assertNotIn(payload, repr(arguments))
        line = options["input"]
        self.assertEqual(line.count(b"\n"), 1)
        self.assertTrue(line.endswith(b"\n"))
        words = shlex.split(line.decode())  # Same backslash/quote cases as Apple's split_line.
        self.assertEqual(words, ["add-generic-password", "-U", "-a", "test-user", "-s", SERVICE, "-w", payload])
        self.assertNotIn("-A", words)
        self.assertNotIn("-T", words)
        self.assertNotIn("-v", words)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], 15)
        self.assertFalse(options.get("shell", False))
        self.inspector.ready.assert_called_once_with(SERVICE, "test-user")

    def test_exact_command_bound_accounts_for_json_escaping_and_metadata(self):
        overhead = len(write_command("user", '{"value":""}'))
        fitting = '{"value":"' + "x" * (MAX_COMMAND - overhead) + '"}'
        self.assertEqual(len(write_command("user", fitting)), MAX_COMMAND)
        self.cli.set_password("user", fitting)
        self.runner.reset_mock()
        for invalid in (fitting.replace("x", "xx", 1), json.dumps({"value": "\\" * 2500}),
                        '{"raw":"秘密"}', '{"raw":"line\nbreak"}', '{}\r', '{}\0', '[]'):
            with self.subTest(length=len(invalid)), self.assertRaises(KeychainPayloadError):
                self.cli.set_password("user", invalid)
        self.runner.assert_not_called()

    def test_control_characters_in_identity_are_rejected_before_process_start(self):
        for value in ("a\nb", "a\rb", "a\0b", "a\tb", "a\x7fb", "a\x85b", "a\u2028b"):
            with self.subTest(value=repr(value)), self.assertRaises(KeychainPayloadError):
                self.cli.get_password(value)
        self.runner.assert_not_called()

    def test_background_denies_unsafe_missing_or_uninspectable_acl_before_cli(self):
        self.inspector.ready.return_value = False
        for operation in (lambda: self.cli.get_password("user", background=True),
                          lambda: self.cli.set_password("user", "{}", background=True),
                          lambda: self.cli.delete_password("user", background=True)):
            with self.assertRaises(KeychainWriteError) as raised:
                operation()
            self.assertEqual(raised.exception.status, -25308)
        self.inspector.ready.side_effect = RuntimeError("FAKE_SECRET")
        with self.assertRaises(KeychainWriteError) as raised:
            self.cli.get_password("user", background=True)
        self.assertNotIn("FAKE_SECRET", str(raised.exception))
        self.runner.assert_not_called()

    def test_foreground_can_approve_and_read_preserves_exact_value(self):
        payload = '{"fake":"credential"}\n'
        self.runner.return_value.stdout = payload.encode() + b"\n"
        self.assertEqual(self.cli.get_password("user"), payload)
        self.inspector.ready.assert_not_called()
        self.assertEqual(self.runner.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 120)

    def test_missing_item_is_none_only_in_foreground(self):
        self.runner.return_value.returncode = 44
        self.assertIsNone(self.cli.get_password("user"))
        with self.assertRaises(KeychainWriteError):
            self.cli.get_password("user", background=True)

    def test_failed_or_timed_out_process_drops_raw_output_and_requires_access(self):
        failures = [SimpleNamespace(returncode=1, stdout=b"FAKE_SECRET"),
                    subprocess.TimeoutExpired([SECURITY], 15, output=b"FAKE_SECRET", stderr=b"FAKE_SECRET"),
                    OSError("FAKE_SECRET")]
        for failure in failures:
            if isinstance(failure, Exception):
                self.runner.side_effect = failure
            else:
                self.runner.side_effect = None
                self.runner.return_value = failure
            for operation in (lambda: self.cli.get_password("user", background=True),
                              lambda: self.cli.set_password("user", "{}", background=True),
                              lambda: self.cli.delete_password("user", background=True)):
                with self.assertRaises(KeychainWriteError) as raised:
                    operation()
                self.assertEqual(raised.exception.status, -25308)
                self.assertNotIn("FAKE_SECRET", str(raised.exception))

    def test_native_service_never_uses_python_secret_reader_or_writer(self):
        api, reader, native = Mock(), Mock(), Mock()
        api.get_user_interaction_allowed.return_value = True
        backend = MacOSKeychain(reader=reader, api=api, native_cli=native)
        with backend.noninteractive():
            backend.get_password(SERVICE, "user")
            backend.set_password(SERVICE, "user", "{}")
            backend.delete_password(SERVICE, "user")
            backend.validate_password(SERVICE, "user", "{}")
        native.get_password.assert_called_once_with("user", background=True)
        native.set_password.assert_called_once_with("user", "{}", background=True)
        native.delete_password.assert_called_once_with("user", background=True)
        native.validate_password.assert_called_once_with("user", "{}")
        reader.get_password.assert_not_called()
        reader.delete_password.assert_not_called()
        api.update.assert_not_called()
        api.add.assert_not_called()
        self.assertFalse(backend._background)


class ACLInspectionTests(unittest.TestCase):
    def setUp(self):
        # These fakes model retained references, borrowed references and pointer
        # outputs so the metadata query/release path runs without any Keychain.
        self.refs, self.owned, self.counter = {}, set(), 1000
        self.partition_ids = ["apple-tool:"]
        self.flags = 0
        self.unlocked = True
        self.app_path = b"/usr/bin/security\0"
        self.valid_signature = True
        self.missing = False
        self.query = None
        self.api = SimpleNamespace(_owned=self.own, _constant=lambda name: name,
                                   _identity=lambda stack, service, username: [("service", service), ("account", username)],
                                   _dictionary=self.dictionary,
                                   get_user_interaction_allowed=lambda: True,
                                   set_user_interaction_allowed=Mock())
        self.guard = object.__new__(NativeACLInspector)
        self.guard.api = self.api
        self.guard.true_ref = 99
        self.guard._array = lambda reference: self.refs[reference] if reference else []
        self.guard._string = lambda reference: self.refs[reference]
        self.guard.copy_item = self.copy_item
        self.guard.copy_keychain = lambda reference, destination: self.copy_to(destination, "keychain")
        self.guard.keychain_status = self.keychain_status
        self.guard.copy_access = lambda reference, destination: self.copy_to(destination, "access")
        self.guard.copy_acls = lambda reference, destination: self.copy_to(destination, [10, 11])
        self.guard.copy_tags = lambda acl: self.allocate([
            self.allocate("ACLAuthorizationPartitionID" if acl == 10 else "ACLAuthorizationDecrypt")])
        self.guard.copy_contents = self.copy_contents
        self.guard.copy_app_data = lambda app, destination: self.copy_to(destination, self.app_path)
        self.guard.data_length = lambda reference: len(self.refs[reference])
        self.guard.data_bytes = lambda reference: self.refs[reference]
        self.guard.validate_app = lambda app, path: 0 if self.valid_signature else -67030

    def allocate(self, value):
        self.counter += 1
        self.refs[self.counter] = value
        return self.counter

    @contextmanager
    def own(self, reference):
        self.assertNotIn(reference, self.owned)
        self.owned.add(reference)
        try:
            yield reference
        finally:
            self.owned.remove(reference)

    @contextmanager
    def dictionary(self, pairs):
        self.query = dict(pairs)
        with self.own(self.allocate(dict(pairs))) as reference:
            yield reference

    def copy_to(self, destination, value):
        ctypes.cast(destination, ctypes.POINTER(ctypes.c_void_p))[0] = self.allocate(value)
        return 0

    def copy_item(self, query, destination):
        return -25300 if self.missing else self.copy_to(destination, "item")

    def keychain_status(self, keychain, destination):
        ctypes.cast(destination, ctypes.POINTER(ctypes.c_uint32))[0] = 1 if self.unlocked else 0
        return 0

    def copy_contents(self, acl, applications, description, flags):
        if acl == 10:
            self.copy_to(description, plistlib.dumps({"Partitions": self.partition_ids}).hex())
        else:
            self.copy_to(applications, [200])
            self.copy_to(description, "Native credentials")
            ctypes.cast(flags, ctypes.POINTER(ctypes.c_uint16))[0] = self.flags
        return 0

    def test_safe_acl_requests_only_metadata_releases_refs_and_restores_ui_policy(self):
        self.assertTrue(self.guard.ready(SERVICE, "user"))
        self.assertIn("kSecReturnRef", self.query)
        self.assertNotIn("kSecReturnData", self.query)
        self.assertEqual(self.query["kSecMatchLimit"], "kSecMatchLimitOne")
        self.assertFalse(self.owned)
        self.assertEqual(self.api.set_user_interaction_allowed.call_args_list[0].args, (False,))
        self.assertEqual(self.api.set_user_interaction_allowed.call_args_list[-1].args, (True,))

    def test_locked_missing_wrong_partition_confirm_or_untrusted_reader_refuse(self):
        cases = (("unlocked", False), ("missing", True), ("partition_ids", ["cdhash:python"]),
                 ("flags", 1), ("app_path", b"/usr/bin/other\0"), ("valid_signature", False))
        for name, value in cases:
            previous = getattr(self, name)
            setattr(self, name, value)
            with self.subTest(name=name):
                self.assertFalse(self.guard.ready(SERVICE, "user"))
                self.assertFalse(self.owned)
            setattr(self, name, previous)

    def test_metadata_failure_is_closed_and_releases_previously_retained_refs(self):
        self.guard.copy_access = lambda reference, destination: -25293
        self.assertFalse(self.guard.ready(SERVICE, "user"))
        self.assertFalse(self.owned)


if __name__ == "__main__":
    unittest.main()
