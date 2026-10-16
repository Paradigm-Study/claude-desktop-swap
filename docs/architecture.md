# Architecture and compatibility

The project joins two existing mechanisms: Desktop's managed process launcher and claude-swap's native credential rotation. It does not implement another credential store or refresh loop.

## State ownership

| State | Owner | What this project changes |
| --- | --- | --- |
| Desktop login and conversation index | Claude Desktop | Nothing; keep the same login/profile |
| Native Code credential and saved account pool | Claude Code / claude-swap | Nothing directly; upstream switches the active credential |
| Process launch configuration | System managed settings + selected Desktop profile | Installs the wrapper and enables the profile's launcher flag |
| Usage sampling and selection | claude-swap | Optional narrowly guarded 0.26.0 decision patch |
| Billing, model entitlements, resource permissions | Anthropic / each account | Nothing |

Desktop ordinarily supplies its own account's credential to the bundled Code process. Updating a separate native login alone does not reliably replace that pinned launch credential. Enabling the managed launcher and removing those overrides lets the bundled process resolve its native credential instead.

The launcher preserves the command, argument boundaries, working directory, and all environment variables except this explicit list for the chosen UID:

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

## Files and privilege

The installer changes the selected user's Desktop profile flag and places a launcher plus managed settings beneath `/Library/Application Support/ClaudeCode/`. The `processWrapper` value is a string encoding a JSON array, as expected by the tested launcher integration. Root permission is needed for the system directory, not for reading or exporting credentials.

The installer records the values it owns so uninstall can restore them while preserving unrelated settings. It refuses a competing process wrapper. It does not rewrite the app bundle, change the Desktop login, copy histories, read the account pool, or access Keychain credentials.

Close Desktop before installation or removal so its own config writes do not race the installer. Restart existing Code processes once to use the changed launch path. The installer supports one user and one selected Desktop profile at a time. To move to a different profile, uninstall the recorded installation first, then install again for the newly selected profile.

## What “preserve sessions” means

Conversation files and Desktop's history identity stay where they are. New turns can use a different native account without creating a new conversation.

This is not a guarantee that every in-flight operation changes accounts immediately. Native authentication is cached, and an API client already retrying a failed request may hold its old bearer. A new turn after the credential cache expires may be needed. Long-running subagents have the same limitation; this integration does not automatically restart them.

Accounts can also have different model entitlements and access to private resources. An account switch does not grant access to artifacts, connectors, or organizations owned by another account. Launcher-based sessions can lose Remote Control support.

## Validation boundary

The launcher mechanism was manually exercised on macOS with bundled Code 2.1.293 and 2.1.295. The public installer's filesystem handling is tested with synthetic fixtures. The optional patch has offline regression tests for stale readings, candidate freshness, and recovery. Those tests do not prove live authentication, quota availability, or compatibility with a future Desktop version.

No public stable API guarantees the selected-profile flag used here. Treat a Desktop upgrade as a reason to repeat the README's real-session verification.
