"""CLI leave ordering with mocked service/native boundaries; no macOS access."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import Mock, patch

from team_pool import client, gui_exec_service


class ClientLeaveTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.native_client = Mock(spec=client.Client)
        self.native_client.leave.side_effect = self.leave
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def leave(self, *, restore):
        self.events.append(("native_leave", restore))
        return {"state": "left", "native_restored": restore}

    def invoke(self, gui_remove, *, restore=True, sync_remove=None):
        if sync_remove is None:
            sync_remove = lambda: self.events.append("sync_removed")
        with (
            patch.object(client.sys, "platform", "darwin"),
            patch.object(client, "Client", return_value=self.native_client),
            patch.object(gui_exec_service, "remove_service", side_effect=gui_remove) as gui,
            patch.object(client, "remove_service", side_effect=sync_remove) as sync,
            redirect_stdout(self.stdout), redirect_stderr(self.stderr),
        ):
            result = client.main(["leave", "--restore"] if restore else ["leave"])
        return result, gui, sync

    def test_busy_gui_service_preserves_sync_and_native_account(self):
        message = "GUI execution has active Claude sessions. Finish those sessions before updating or removing it."

        def busy():
            self.events.append("gui_busy")
            raise gui_exec_service.GuiServiceError(message)

        result, gui, sync = self.invoke(busy)
        self.assertEqual(result, 2)
        self.assertEqual(self.events, ["gui_busy"])
        gui.assert_called_once_with()
        sync.assert_not_called()
        self.native_client.leave.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), message + "\n")

    def test_successful_gui_drain_precedes_sync_removal_and_restore(self):
        def drained():
            self.events.append("gui_drained_and_removed")
            return {"status": "removed"}

        result, gui, sync = self.invoke(drained)
        self.assertEqual(result, 0)
        self.assertEqual(self.events, ["gui_drained_and_removed", "sync_removed", ("native_leave", True)])
        gui.assert_called_once_with()
        sync.assert_called_once_with()
        self.native_client.leave.assert_called_once_with(restore=True)
        self.assertEqual(json.loads(self.stdout.getvalue()), {"state": "left", "native_restored": True})
        self.assertEqual(self.stderr.getvalue(), "")

    def test_absent_gui_service_keeps_older_installations_compatible(self):
        def absent():
            self.events.append("gui_absent")
            return {"status": "absent"}

        result, _, sync = self.invoke(absent)
        self.assertEqual(result, 0)
        self.assertEqual(self.events, ["gui_absent", "sync_removed", ("native_leave", True)])
        sync.assert_called_once_with()
        self.native_client.leave.assert_called_once_with(restore=True)
        self.assertEqual(self.stderr.getvalue(), "")

    def test_leave_without_restore_still_drains_before_retaining_recovery(self):
        def drained():
            self.events.append("gui_drained_and_removed")
            return {"status": "removed"}

        result, _, _ = self.invoke(drained, restore=False)
        self.assertEqual(result, 0)
        self.assertEqual(self.events, ["gui_drained_and_removed", "sync_removed", ("native_leave", False)])
        self.native_client.leave.assert_called_once_with(restore=False)

    def test_sync_removal_failure_does_not_restore_under_running_sync(self):
        def drained():
            self.events.append("gui_drained_and_removed")
            return {"status": "removed"}

        def failed_sync():
            self.events.append("sync_removal_failed")
            raise client.ClientError("Fixture sync removal refused.")

        result, _, _ = self.invoke(drained, sync_remove=failed_sync)
        self.assertEqual(result, 2)
        self.assertEqual(self.events, ["gui_drained_and_removed", "sync_removal_failed"])
        self.native_client.leave.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), "Fixture sync removal refused.\n")


if __name__ == "__main__":
    unittest.main()
