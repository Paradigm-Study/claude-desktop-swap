# Architecture and compatibility

The project connects Desktop's managed process launcher to Claude Code's native
subscription credential store. Personal mode uses claude-swap for account
rotation. The optional team mode uses a coordinator to own pooled account refresh
credentials and assign short-lived access credentials to enrolled Macs. Code
inference goes directly to Anthropic in both modes.

## State ownership

| State | Owner | What this project changes |
| --- | --- | --- |
| Desktop login and conversation index | Claude Desktop | Nothing; keep the same login/profile |
| Native Code account credential | Claude Code's native Keychain item | Personal claude-swap or the team client replaces the active subscription credential; only one selector should run |
| Local MCP/plugin sign-ins | Claude Code's native Keychain item | Retains them through account changes; never sends them to the pool |
| Shared account refresh credentials and selection | Team coordinator | Refreshes pooled accounts and issues access-only assignments to enrolled devices |
| Device identity and recovery backup | Each enrolled macOS user's Keychain | Stores private team client items separately from the native Code item |
| Process launch configuration | System managed settings + Desktop's local configuration library | Installs the wrapper and enables the selected configuration's launcher flag |
| Remote process execution | Desktop SSH helper + per-user GUI LaunchAgent | Relays the native Code process through that same user's GUI login context |
| Personal usage sampling and selection | claude-swap | Optional narrowly guarded 0.26.0 decision patch |
| Billing, model entitlements, resource permissions | Anthropic / each account | Nothing |

Desktop ordinarily supplies its own account's credential to the bundled Code process. Updating a separate native login alone does not reliably replace that pinned launch credential. Enabling the managed launcher and removing those overrides lets the bundled process resolve its native credential instead.

The launcher preserves the command, argument boundaries and working directory.
For each enrolled UID, it clears this explicit list before local execution or
remote relay:

```text
CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST
CLAUDE_CODE_HOST_AUTH_ENV_VAR
CLAUDE_CODE_OAUTH_TOKEN
CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR
ANTHROPIC_AUTH_TOKEN
ANTHROPIC_API_KEY
ANTHROPIC_BASE_URL
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
```

`--restore-fast-control` also clears `CLAUDE_CODE_DISABLE_FAST_MODE`. It does not bypass server-side entitlement checks. Desktop-provided account/organization metadata is otherwise preserved, so displayed labels or account-scoped integrations can differ from the native inference account.

The team remote relay applies additional environment checks described below;
the list above is the local wrapper's behavior, not its entire remote policy.

## Files and privilege

The installer places a launcher plus managed settings beneath
`/Library/Application Support/ClaudeCode/`. The `processWrapper` value is a string
encoding a JSON array, as expected by the launcher integration. Root permission
is needed for the system directory, not for reading or exporting credentials.

In a14, the launcher flag lives in the user's persistent Desktop configuration
library, `~/Library/Application Support/Claude-3p/configLibrary/`. An existing
selected configuration retains its selection and other settings. For an
initialized ordinary Desktop installation without that optional library, setup
creates Desktop's default library metadata and a configuration containing only
`claudeCodeProcessWrapperEnabled: true`. It does not set an inference provider,
bootstrap URL, deployment mode or replacement user-data directory. The ordinary
`Claude/config.json` remains an initialization marker, not the flag's storage.

Earlier installers wrote a macOS-generated managed-preference cache that could
disappear. An explicit setup rerun migrates only this user's recorded legacy
flag to the local library, including when that cache is gone. Externally managed
policy remains authoritative; setup refuses a disabling policy or an unrelated
managed configuration rather than replacing it.

The privileged launcher installer records the values it owns so uninstall can
restore them while preserving unrelated settings. Its durable transaction
journal allows a rerun to recover a recognized interrupted change. It refuses a
competing process wrapper. This component does not rewrite the app bundle,
change the Desktop login, copy histories, read the account pool or access
Keychain credentials; the separately running team client handles enrollment and
native credential updates.

Close Desktop before installation or removal so its own config writes do not
race the installer. Restart existing Code processes once to use the changed
launch path. Multiple macOS users can enroll independently; the shared wrapper
retains the other users' records when one is added or removed. Each user has one
recorded Desktop configuration. To move that user to a different configuration,
uninstall their recorded installation before installing for the new selection.

## Native execution in SSH sessions

Desktop's SSH helper can start outside the macOS login context that can read the
user's Keychain. Restarting a helper inside that context fixes only that helper
instance. The a14 team launcher instead routes executables in the enrolled
user's `~/.claude/remote/ccd-cli/` directory through
`study.paradigm.claude-desktop-swap.gui-exec`, a GUI LaunchAgent for the same UID.
Desktop still owns its helper and conversation records.

The bridge's private Unix socket authenticates the peer UID. It validates the
owned executable and working directory, restores the enrolled user's home and
identity environment, and removes provider/configuration overrides and unsafe
loader variables. It preserves the remaining tool environment. A noninteractive
local readiness guard checks the current assignment and native ownership before
the GUI service launches Code, which reads its usual native Keychain credential.
The protocol relays process arguments, environment, stdin, stdout, stderr,
signals and exit status. It does not fetch or transmit account credentials.

Each relay owns its child process group; disconnect or service shutdown cancels
that group. A healthy identical setup leaves active jobs running. Upgrades and
removal require an authenticated idle drain with a process-identity check, and
refuse active jobs instead of interrupting them. The installer retains recovery
files if it cannot safely roll back a failed replacement.

The enrolled macOS user must remain logged into a GUI session. Fast User
Switching can leave it running, but logout stops the GUI services and their
processes. SSH login alone does not establish this context. Missing GUI service,
unavailable Keychain or expired assignment fails with a local setup error;
there is no unauthenticated or headless fallback.

## Redundant connector-file recovery

Claude can leave an MCP-only `~/.claude/.credentials.json` after native access
fails. The team client keeps Keychain authoritative. For an already managed
enrollment, it can archive this file only when it contains solely `mcpOAuth` and
every connector's fields match its Keychain entry except `discoveryState`.
Keychain's discovery metadata also remains unchanged.

Recovery requires the expected account fingerprint, existing enrollment backup,
safe file ownership and unchanged native data. It archives the original bytes
under `~/.claude/.desktop-swap-fallback/` using a durable metadata-only journal,
then verifies the archive and native state before clearing that journal. It does
not merge new sign-ins, rewrite Keychain or discard connector data. Conflicts,
unsupported contents or ambiguous interruption state stop recovery with the
files retained. Read-only status and catalog requests never perform this repair.

## What “preserve sessions” means

Conversation files and Desktop's history identity stay where they are. New turns can use a different native account without creating a new conversation.

This is not a guarantee that every in-flight operation changes accounts immediately. Native authentication is cached, and an API client already retrying a failed request may hold its old bearer. A new turn after the credential cache expires may be needed. Long-running subagents have the same limitation; this integration does not automatically restart them.

Accounts can also have different model entitlements and access to private resources. An account switch does not grant access to artifacts, connectors, or organizations owned by another account. Launcher-based sessions can lose Remote Control support.

## Validation boundary

The original launcher mechanism was manually exercised on macOS with bundled
Code 2.1.293 and 2.1.295. The installer and recovery paths use synthetic filesystem
tests. The GUI relay tests use local subprocesses for stream handling,
cancellation and isolation, while service lifecycle tests simulate launchd.
These checks do not prove live a14 Keychain access, authentication, quota
availability or compatibility with a future Desktop version.

Run the installed `team-pool-client doctor --ssh` as the enrolled user on the
remote Mac for a read-only diagnosis of GUI login, sync, assignment and bridge
readiness. It makes no provider request and does not prove that the original
conversation works. Verify the complete path by retrying a normal-speed message
in that same remote conversation. See [the SSH contract](ssh-native-mode.md) and
the [pilot acceptance record](team-pilot-acceptance.md) for validation evidence.

No public stable API guarantees the selected-profile flag used here. Treat a Desktop upgrade as a reason to repeat the README's real-session verification.
