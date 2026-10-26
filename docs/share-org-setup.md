# Join the native pool with Claude Share

Members of the linked **Paradigm** team in Claude Share can enroll their Mac
using their existing Share sign-in. No invitation token or separate pool account
is needed. Claude Desktop Code continues to use native subscription access;
inference goes directly to Anthropic.

## One-time Mac setup

You need macOS, Claude Desktop with local Code initialized, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). In Claude Code, open
`/team`, sign in to Claude Share, and confirm that Paradigm is listed. A shared
session link alone does not establish team membership.

Quit Claude Desktop for this initial setup, then run in Terminal:

```sh
git clone --single-branch --branch codex/team-pool \
  https://github.com/Paradigm-Study/claude-desktop-swap.git
cd claude-desktop-swap
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python .
sudo .venv/bin/python scripts/desktop_swap.py install --user "$USER"
```

If you previously used the personal `cswap` rotator, stop its `cswap auto` loops
and disable its menu-bar service before continuing:

```sh
launchctl disable "gui/$(id -u)/com.cswap.menubar"
launchctl bootout "gui/$(id -u)/com.cswap.menubar"
```

The second command may say the service does not exist on a fresh Mac. Use a
terminal without Claude gateway or API-key environment overrides, then run:

```sh
.venv/bin/team-pool-client enroll-share
.venv/bin/team-pool-client sync --once --take-over-native-store
.venv/bin/team-pool-client service-install
python3 scripts/install_team_status.py install
.venv/bin/team-pool-client status
```

The first command reads the private Share sign-in already saved on this Mac.
If Share keeps its sign-in in another configuration folder, supply
`enroll-share --account-file /absolute/path/shared-session/account.json`.
The native pilot supports the default Claude credential store only; run setup
without a `CLAUDE_CONFIG_DIR` override even when selecting another Share file.
The sync command explicitly replaces the local Code subscription credential and
keeps a private recovery backup. Approve the expected macOS Keychain request for
`security` to access `Claude Code-credentials` if one appears. Use client
0.1.0a6 or newer; the earlier repeating approval defect is described in
[Keychain recovery](keychain-access.md).

Keep this checkout and its virtual environment in place. Reopen Claude Desktop,
resume an existing local Code conversation, and send a normal-speed message.
The **Claude Swap** menu bar shows the pool account and background sync status.
Fast mode retains its separate paid-credit requirements.

## A Mac already enrolled by invitation

Upgrade the installed client, then attach its existing device to Share membership:

```sh
.venv/bin/team-pool-client service-remove
git pull --ff-only
uv pip install --python .venv/bin/python .
.venv/bin/team-pool-client link-share
.venv/bin/team-pool-client sync --once
.venv/bin/team-pool-client service-install
```

Linking keeps the existing device token, native login, and recovery backup.
It refuses a different pool or a later attempt to bind another Share identity.

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
