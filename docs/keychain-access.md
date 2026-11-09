# Repeated Keychain approval in the native pilot

Client 0.1.0a4 had a live integration defect: every sync rewrote the native
credential through Python, including unchanged assignments. A successful write
and inference request did not establish that later Desktop requests would remain
silent. The user reported repeated approvals even after choosing Always Allow.

## Demonstrated cause

On the affected Mac, both account-switching agents were stopped. Process tracing
still observed Desktop's bundled Claude Code launching `/usr/bin/security`.
The native item's ordinary application ACL trusted both the current Python
interpreter and `/usr/bin/security`, and the latter's code requirement validated.
However, the separate partition ACL allowed only Python's current code hash.

Apple's data-update implementation creates a new secure-storage group and removes
the previous partition and integrity ACLs. The new partition belongs to the
writer. Thus `SecItemUpdate` preserved the ordinary app list but undid the
partition approval that Desktop needed on every write. A separately signed helper
would reproduce this conflict under its own identity.

Sources: [Apple's data update implementation](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/Item.cpp#L903-L985),
[the security tool's apple-tool identity](https://github.com/apple-oss-distributions/Security/blob/main/securityd/src/clientid.cpp#L260-L285),
and [partition approval](https://github.com/apple-oss-distributions/Security/blob/main/securityd/src/acls.cpp#L206-L243).

## Correction in 0.1.0a5

Only the `Claude Code-credentials` item uses Apple's `/usr/bin/security` for reads
and writes, matching Desktop. Private pool device credentials and recovery backups
remain in their own Python-owned Keychain items. Unchanged assignments update
operational status without rewriting either native credentials or `.claude.json`,
after the normal policy, drift, expiry and backup checks.

Native writes use one quoted command through stdin, never a secret in process
arguments, logs, or a temporary file. The complete command must fit 4032 bytes.
The client validates this before creating a backup or write journal; larger
MCP/plugin credential sets pause with `native_store_capacity`. No fields are
discarded to make the command fit. Writes are verified by exact readback.

Background private-item operations disable Keychain interaction. Native operations
first inspect lock state, partition permissions, and the trusted security tool
without decrypting the item. An authorization failure or timeout pauses the
client instead of retrying. The security command has no no-UI flag: a lock or ACL
change between preflight and access can still produce a prompt before the timeout
stops the attempt. This is not a promise that macOS can never request approval.

## Upgrade and recovery

Use the [one-command setup](share-org-setup.md) in Terminal on the Mac's screen,
as the enrolled macOS user. It updates the pinned client, launcher, sync service
and GUI execution service together. It preserves the enrollment and recovery
backup and verifies background sync. Do not run the personal `cswap` selector
alongside the team client. Finish and close remote Code sessions using the bridge
before upgrading its service; setup refuses to interrupt its running processes.

Use a terminal without gateway/API-key overrides. During the explicit foreground
step, macOS may request approval for `security` to access `Claude Code-credentials`.
Choose Always Allow only for that expected request. Never paste a Mac password
into chat or put it in a command argument. The client does not request or store
the Keychain password, unlock Keychain, or enable access for all applications.

The shared menu bar displays `keychain_access_required` as a paused condition.
After correcting access, use `resume`, `sync --once`, and restart the service.
If prompts recur, stop the service and inspect the actual caller and item before
making further changes. Do not restore the Python native writer from 0.1.0a4.

## Remote Mac login and SSH context

A readable Keychain in the user's desktop session does not imply that a process
started through SSH can read it. Desktop can replace or cold-start its SSH
helper in a different macOS audit context. A GUI-context helper restart was a
temporary repair for that instance, not a durable authentication mechanism.

The a14 team launcher sends remote Code execution through the enrolled user's
GUI LaunchAgent. Its same-UID private socket relays process input/output, signals
and exit status; it does not export provider credentials or pass them through the
protocol. The GUI service checks local native readiness without requesting
Keychain interaction, then launches Code in its GUI context to use the ordinary
native credential store. Inference still goes directly to Anthropic.

The enrolled macOS user must stay logged into the remote Mac's desktop. Fast
User Switching can leave that session running. Logout stops both GUI sync and
execution; an SSH sign-in alone cannot replace them or unlock the login Keychain.
The bridge fails with a setup message when its native prerequisites are absent.
It does not change Keychain ACLs, store a password or provide access after logout.

Run the installed client as that user, without `sudo`, for a read-only check:

```sh
team-pool-client doctor --ssh
```

Use the installed client path printed by setup if it is not on `PATH`. The
diagnostic checks the GUI session, sync service, assignment and GUI bridge, with
noninteractive native checks. It does not contact the coordinator or provider,
change credentials, or display credential values. When the bridge is ready,
direct SSH Keychain access is not required and is not reported as an error.
Retry the original remote conversation to verify actual authentication; a
readiness report is not that end-to-end test.

## Launcher policy and redundant connector files

An older installer's flag could disappear because it was stored in a generated
macOS managed-preference cache. A14 setup migrates the recorded setting into
Desktop's persistent local `configLibrary`. An ordinary Desktop installation
without that library gets only the default metadata and launcher flag, retaining
its login, conversation data and deployment mode. Existing selected settings and
administrator policy are respected. This local policy failure does not establish
that a pooled account needs fresh authorization.

A failed native read can also leave `~/.claude/.credentials.json` containing MCP
sign-ins. The a14 managed client can retire this fallback only when its sole
top-level field is `mcpOAuth` and every connector already matches Keychain except
for `discoveryState`. Keychain remains authoritative, including its discovery
metadata. The original file is archived unchanged under the private
`~/.claude/.desktop-swap-fallback/` directory; native account credentials,
connector data and the enrollment backup are not rewritten by this recovery.

Recovery checks account ownership and backup presence, journals the archive
operation, and verifies both the archived bytes and native state. An interrupted
or ambiguous archive keeps ordinary sync blocked until verified recovery.
New connectors, changed sign-in fields, account credentials in the fallback,
unsafe files or credential drift are retained for review. This does not solve
`native_store_capacity` by removing MCP/plugin data, nor does it authorize
overwriting a manual login. Treat fallback archives as credentials and never
attach them to support reports.

An explicit setup rerun can repair an owned `native_setup_or_policy` pause after
these ownership checks. Other pauses require their specific condition to be
resolved. Read-only status, catalog and `doctor --ssh` checks do not archive files
or resume a paused client. See [the SSH contract](ssh-native-mode.md) for the
execution and recovery boundaries.
