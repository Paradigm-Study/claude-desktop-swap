# Join the native pool with Claude Share

Members of the linked **Paradigm** team in Claude Share can enroll their Mac
using their existing Share sign-in. No invitation token or separate pool account
is needed. Claude Desktop Code continues to use native subscription access;
inference goes directly to Anthropic.

## One-command Mac setup

In a local Claude Desktop Code conversation, run `/team`, sign in to Claude
Share, and confirm that **Paradigm** is listed. A shared session link alone does
not establish team membership. Then paste this command into Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/Paradigm-Study/claude-desktop-swap/codex/team-pool/install.sh | /bin/bash
```

Run it as your ordinary Mac user, without `sudo` in front of the command. The
installer uses the tested `v0.1.0a9` package and prepares its own Python runtime;
Git, Python and uv do not need to be installed beforehand. If Apple Command Line
Tools are missing, it opens their installer and continues when you finish it.

Setup checks your saved Share sign-in and current Paradigm membership, installs
the native launcher if needed, enrolls or links this Mac, starts background sync,
and installs the **Claude Swap** menu bar. An existing correct launcher skips
administrator approval and restarting Desktop. A first installation gracefully
quits and reopens Desktop if it was running; it does not force-quit a busy app.
The original Desktop profile and conversation index are preserved. The native
Code credential is replaced with the assigned subscription credential, with a
private recovery backup retained.

macOS may ask for administrator approval. If Keychain asks for **`security` to
access `Claude Code-credentials`**, choose **Always Allow** for that exact request.
A one-time Allow does not authorize later background syncs. Setup verifies a real
background sync before reporting success and pauses on access failures instead
of repeatedly prompting. See [Keychain recovery](keychain-access.md).

After setup, resume an existing local Code conversation and send a normal-speed
message. The menu bar lists all shared accounts, their 5-hour and weekly usage, and the account assigned to this Mac. Open an available account’s submenu to switch this Mac; automatic quota failover continues. Choose **Add Account to Shared Pool…** to authorize another subscription in the browser, then **Enter Authorization Code…** to finish in its hidden field. See the [menu guide](team-status-menu.md). Fast mode retains
its separate paid-credit requirements. A second physical Mac still needs live
verification in the [pilot acceptance worksheet](team-pilot-acceptance.md).

## If an older setup reported “Missing _meta.json”

That error came from assuming Desktop's optional profile library existed. The
`v0.1.0a9` installer supports an initialized ordinary Claude Desktop installation
without that library. Open Claude Desktop, finish its normal first sign-in if
needed, and run the same one-command setup again. Do not create a replacement
profile or delete your existing app data.

For a standard installation, setup enables Desktop's supported user-scoped
launcher preference. Existing profile installations keep their current profile.
It preserves the app's sign-in, conversation index and deployment mode. A managed
policy that disables the launcher must be resolved by its administrator. Setup checks Desktop initialization before stopping sync or closing Desktop.
If an older installer left a recognized system directory unreadable, a9 first
verifies Share membership and asks for administrator approval to repair that
directory. The repair checks its contents and ownership, preserves private file
permissions, and refuses unrelated files. It runs before native credential
inspection, so the filesystem failure is no longer mislabeled as a Keychain
problem. The Terminal `Password:` prompt is the Mac administrator
password requested by `sudo`; it is separate from Claude sign-in and Keychain.

## Repeat setup, upgrade, or link an existing device

Run the same command again. It reuses the installed release, preserves the
existing device token and native recovery backup, and links an existing invite
enrollment to your Share identity. It refuses a different Share identity,
manual credential drift, or a paused/revoked/left enrollment until you explicitly
resolve that condition. A failed installation can be retried with the same
command; it does not require a second enrollment or invitation token.

The installer stops the personal `cswap` menu service to avoid competing writes.
If you run `cswap auto` in a Terminal, stop that loop first. Global terminal and
Codex gateway settings are preserved. Native setup supports the default Claude
credential store only and refuses a `CLAUDE_CONFIG_DIR` override or conflicting
native provider configuration.

The package and runtime stay under
`~/.local/share/claude-desktop-swap/releases/v0.1.0a9/`. Keep that directory in
place while the service uses it. Setup prints the installed client path; for this
release, status and contribution commands are:

```sh
env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL ~/.local/share/claude-desktop-swap/releases/v0.1.0a9/venv/bin/team-pool-client status
env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL ~/.local/share/claude-desktop-swap/releases/v0.1.0a9/venv/bin/team-pool-client contribute-account
```

If Share saves its sign-in elsewhere, use the same installer with its account
file path. The native credential store must still be the default one:

```sh
curl -fsSL https://raw.githubusercontent.com/Paradigm-Study/claude-desktop-swap/codex/team-pool/install.sh | /bin/bash -s -- --account-file /absolute/path/shared-session/account.json
```

For a local readiness check, pass `--check` instead. It may prepare the downloaded
runtime, but does not change Claude, enroll, or contact Share. The check requires
noninteractive access to the current Keychain items; ordinary setup can request
foreground authorization when needed. Readiness is not a successful enrollment.

## What membership controls

Share checks the exact configured team and its current membership record. It
does not authorize from an email domain, a cached team selection, or the team's
session-link access setting. The saved Share bearer is sent only to its own
HTTPS server. The pool receives a short-lived, one-use grant bound to a secret
held by this Mac, then stores only the Share account and team identifiers.

The coordinator checks current membership before delivering assignments or
handling contributions, and again after provider operations. Removing a person
from the Share team stops future credential delivery. Credentials already sent
to a Mac remain valid until Anthropic expires or revokes them. The organization
directory being unavailable stops new deliveries without revoking the device.
Changes in a linked upstream GitHub organization do not automatically remove a
person from Share; remove their Share team membership explicitly.

If sign-in expired, use `/team` to sign in again and repeat enrollment. If an
enrollment response was lost, repeat setup to obtain a new grant; grants are
never replayed. An orphaned device from the lost response can be revoked by an
administrator. A member who was removed needs a new device enrollment after
being readmitted. Contribution still works through
`team-pool-client contribute-account` on an enrolled Mac.

The Share deployment sets `NATIVE_POOL_TEAM_ID` and `NATIVE_POOL_URL`; the pool
sets matching `POOL_SHARE_TEAM_ID` and `POOL_PUBLIC_URL`. Its `SHARE_DIRECTORY`
binding points to the existing Share `DirectoryObject` and singleton `directory`.
Deploy Share before the pool. The two private membership/grant RPCs are never
forwarded by Share's public Worker.
