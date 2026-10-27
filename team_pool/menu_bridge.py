"""Private stdin/stdout protocol for the native pool menu; never pass secrets in argv."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from .client import Client, ClientError, SERVICE
from .catalog import identifier
from .contribute import Contribution
from .native import NativeStoreError

MAX_INPUT = 8192
COMMANDS = {"list": set(), "switch": {"account_id"}, "contribute-start": set(),
            "contribute-complete": {"contribution_id", "code"},
            "contribute-status": set(), "contribute-cancel": set()}


def service_running():
    result = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{SERVICE}"],
                            capture_output=True, text=True, timeout=5, check=False)
    return result.returncode == 0 and bool(re.search(r"(?m)^\s*pid = \d+$", result.stdout))


def dispatch(request, client, *, running=service_running):
    if (not isinstance(request, dict) or not isinstance(request.get("action"), str)
            or request["action"] not in COMMANDS
            or set(request) != {"action"} | COMMANDS[request["action"]]):
        raise ClientError("Unrecognized menu request; no account was changed.")
    action = request["action"]
    with client.native.noninteractive():
        if action == "list":
            return client.list_accounts()
        if action == "switch":
            if not identifier(request["account_id"]):
                raise ClientError("Select a valid account from the shared pool; no account was changed.")
            if not running():
                raise ClientError("Background synchronization is stopped. Run the setup command before switching accounts.")
            return client.sync(account_id=request["account_id"])
        flow = Contribution(client)
        if action == "contribute-start":
            return flow.start()
        if action == "contribute-complete":
            if not isinstance(request["contribution_id"], str):
                raise ClientError("This contribution is invalid. Check Add Account status before continuing.")
            return flow.complete(request["contribution_id"], request["code"])
        return flow.inspect(cancel=action == "contribute-cancel")


def main(*, input_stream=None, output_stream=None, factory=lambda: Client(lock_timeout=2)):
    input_stream, output_stream = input_stream or sys.stdin, output_stream or sys.stdout
    try:
        if sys.platform != "darwin":
            raise ClientError("The native pool menu requires macOS.")
        # Scope these removals to the helper. Global shell/Codex routing is unchanged.
        for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            os.environ.pop(key, None)
        raw = input_stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ClientError("The menu request is too large; no account was changed.")
        try:
            request = json.loads(raw)
        except (ValueError, TypeError):
            raise ClientError("The menu request could not be read; no account was changed.") from None
        result = dispatch(request, factory())
        reply = {"ok": True, "result": result}
    except ClientError as error:
        reply = {"ok": False, "error": str(error)}
    except NativeStoreError:
        reply = {"ok": False, "error": "Keychain access needs attention. Run setup in Terminal to approve access; the menu will not repeatedly prompt."}
    except Exception:
        # Provider, backend and OS exceptions may embed request material.
        reply = {"ok": False, "error": "The menu operation could not complete. Refresh status before retrying; existing recovery state was retained."}
    output_stream.write(json.dumps(reply) + "\n")
    return 0 if reply["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
