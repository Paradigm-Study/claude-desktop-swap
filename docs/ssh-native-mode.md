# Native subscriptions in Desktop SSH sessions

Use the ordinary [one-command setup](share-org-setup.md) in Terminal on the remote
Mac's screen, as the macOS user that SSH connects to. Each user must enroll
independently and remain logged into a desktop session. Native Code inference
still goes directly to Anthropic; this is not an API gateway.

## Why the previous repair did not persist

The sync agent ran in the user's GUI login and stopped at logout. Desktop's SSH
helper could start in a different macOS audit context and fail to read the login
Keychain, even while the GUI session could read it. The managed launcher had
already suppressed Desktop's ordinary host credential, so those processes had
no working account. Restarting a helper in GUI context repaired one instance;
an update or cold start could recreate the failure.

Two additional conditions stopped later sync: the installer wrote its flag into
a macOS-generated managed-preference cache, and a connector-only credential file
could reappear after Keychain access failed. They were local lifecycle defects,
not proof that the shared subscription accounts needed fresh authorization.

## a14 execution contract

The installer stores the launcher flag in Desktop's local configuration library
and records its original values for reversal. A new flag-only configuration does
not set an inference provider, bootstrap URL, deployment mode, or replacement
user-data directory. Existing selected configurations and externally managed
policy remain authoritative.

The team launcher routes this user's `~/.claude/remote/ccd-cli/` executable through
a per-user GUI execution service. The service runs the actual Code process in
the logged-in user's context. Its private Unix socket checks the connecting UID;
requests from other macOS users are refused. It validates the executable, resets
the account/configuration environment, and verifies a current native assignment
before launching. Process input/output, signals and exit status use the relay.
Native Code reads its credentials through the usual Keychain path. The service
also checks native ownership before launch; credentials never travel in the
bridge protocol or get stored in its runtime files.

Desktop continues to manage its own SSH helper and conversation records. The
bridge does not poll for or replace that helper. Disconnecting a relay cancels
only its corresponding child process. Service upgrades refuse active jobs rather
than interrupting conversations; finish and close affected remote Code sessions,
then rerun setup. A session process may remain alive between turns.

Logging out still stops GUI services and ends their processes. Keep the enrolled
macOS user logged in; Fast User Switching can leave that session running. A
missing service, inaccessible Keychain or expired assignment fails with a local
setup message instead of launching unauthenticated Code. This release does not
provide headless access after logout or transfer refresh credentials to the Mac.

## Connector-file recovery

An already managed client can retire an MCP-only fallback file when every
connector's authentication fields already match Keychain. Keychain remains
authoritative, including its discovery metadata. The original file is archived
unchanged in the private `~/.claude/.desktop-swap-fallback/` directory; its
metadata-only journal blocks sync until the archive is verified. Treat those
archives as credentials and do not attach them to support reports.

New connectors, changed authentication fields, unsafe files, manual login drift,
missing recovery backup, or conflicting native policy stop automatic recovery.
No account credentials, MCP data or plugin data are discarded to make a write
fit. Read-only status/catalog commands never perform this recovery.

## Diagnose and verify

Run the installed client's `doctor --ssh` on the remote Mac as the enrolled user.
It returns fixed categories for the GUI login, sync service, assignment and
Keychain/bridge readiness. It does not contact the provider or display credential
values. A readable Keychain in one process is not proof that the original remote
conversation works: retry a normal-speed message in that same conversation.

For setup-policy pauses, rerun the ordinary setup command in the desktop Terminal.
It validates existing enrollment ownership before repairing recognized settings
or redundant connector files. Do not reconnect pooled accounts for a local GUI
or Keychain problem. Actual provider authorization rejection remains a separate
account-recovery flow.
