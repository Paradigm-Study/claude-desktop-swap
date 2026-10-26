# Native team pool on Cloudflare

The pool runs as a separate Worker and one SQLite Durable Object in an existing
Cloudflare account. It can share the account used by Claude Share, but has its own
Worker, database, encryption key, administrator credential, and invitations.
Claude Share's rooms, containers, routes, and authentication are not involved.

This is the experimental two-Mac pilot. Automated tests use fake provider
credentials. Real browser authorization has succeeded on the first Mac; provider
renewal and Desktop continuity on both Macs remain in the
[acceptance worksheet](team-pilot-acceptance.md).

## What Cloudflare handles

The coordinator stores encrypted subscription credentials, owns their refresh
operations, issues one-use invitations, accepts account contributions, and assigns
access tokens to enrolled Macs. Inference goes directly from Claude Desktop Code
to Anthropic. No API-key billing, inference gateway, or Fast-mode enablement is
introduced. Fast remains subject to the provider's separate paid-credit rules.

The singleton `team-pool-v1` serializes provider grants. It commits an in-flight
marker before any token exchange and commits successors before delivery. After
an interrupted grant, it quarantines that account or marks the contribution
uncertain rather than replaying a possibly consumed credential.

Credentials and pending PKCE verifiers are AES-GCM encrypted with an independent
Worker secret. Invitation and device tokens are stored as hashes. Only access
tokens are delivered to Macs. Revoking a device stops future deliveries; it
cannot revoke a provider token already delivered. Administrators who can change
Worker code or read its secrets remain trusted with the whole pool.

## Deploy

Use Node.js 22 or newer, Python 3.12 or newer, and an authenticated Wrangler
session for the intended Cloudflare account. Worker/Durable Object usage is billed
to that account. No container or AWS host is required.

```sh
cd cloudflare
npm ci
npm test
npm run check
```

Choose a unique Worker name in `wrangler.toml`. Set `CLOUDFLARE_ACCOUNT_ID` to the
intended account when deploying. `POOL_SWITCH_THRESHOLD` currently uses 98 percent;
change it before deployment if the pool should switch earlier. Polling runs on
assignment requests, normally no more often than once per 120 seconds per account.
If every account is stale or exhausted, an already assigned Mac keeps its valid
token with a degraded status; a new assignment fails instead of inventing capacity.

Generate independent secrets in a private directory outside the repository:

```sh
mkdir -p "$HOME/.config/claude-desktop-swap"
chmod 700 "$HOME/.config/claude-desktop-swap"
python3 - <<'PY'
import base64, json, os, secrets
from pathlib import Path
path = Path.home() / '.config/claude-desktop-swap/worker-secrets.json'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump({'POOL_MASTER_KEY': base64.b64encode(secrets.token_bytes(32)).decode(),
               'POOL_ADMIN_TOKEN': secrets.token_urlsafe(32)}, output)
PY
npm run deploy
npx wrangler secret bulk "$HOME/.config/claude-desktop-swap/worker-secrets.json"
```

Before secrets are installed, the Worker returns `503 coordinator-not-configured`.
Never replace `POOL_MASTER_KEY` on an existing database: existing credentials would
become unreadable. Retain its private backup. Do not rename the Durable Object
binding, class, or singleton identity during a routine update.

Create a private `cloudflare-admin.json` alongside the secret file with exactly
`server` (the deployed HTTPS origin) and `admin_token` (the value of
`POOL_ADMIN_TOKEN`). Keep its permissions at `0600`. Do not put tokens in shell
arguments, source control, shared documents, or logs.

From the repository root:

```sh
python3 scripts/cloudflare_pool_admin.py accounts
python3 scripts/cloudflare_pool_admin.py devices
python3 scripts/cloudflare_pool_admin.py invite --out "$HOME/teammate-1.invite"
```

Each invitation is single-use and lasts 24 hours by default. Give each teammate
only their own file through a private channel. The admin CLI never prints the
invitation itself. `GET /health` returns service status without account/device data.

## Teammate setup

Each teammate needs macOS, Claude Desktop with local Code initialized, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). Keep their original
Desktop login so their existing conversation index remains visible. Quit Desktop
for the initial setup, then run:

```sh
git clone --single-branch --branch codex/team-pool \
  https://github.com/Paradigm-Study/claude-desktop-swap.git
cd claude-desktop-swap
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python .
sudo .venv/bin/python scripts/desktop_swap.py install --user "$USER"
```

Keep this folder in place; the background agent uses its virtual environment.
If `cswap`'s menu-bar service is running, unload it, and stop any `cswap auto`
loops. A teammate does not need to install `cswap` to use the team client.

```sh
launchctl disable "gui/$(id -u)/com.cswap.menubar"
launchctl bootout "gui/$(id -u)/com.cswap.menubar"
```

The unload command may report that the service does not exist on a fresh Mac.
Disabling it also prevents it from restarting at the next login.
Use a terminal without Claude gateway/API-key environment overrides. Replace
the coordinator URL below with the verified deployment URL:

```sh
.venv/bin/team-pool-client enroll \
  --server https://YOUR-POOL.YOUR-SUBDOMAIN.workers.dev \
  --device-name "$(scutil --get ComputerName)"
```

Paste the one-use invitation into the hidden prompt. Alternatively, pass
`--invitation-file /absolute/path/teammate-1.invite` after setting that file to
mode `0600`. Enrollment itself does not change native credentials.

If the pool is empty, contribute its first account before syncing. Otherwise:

```sh
.venv/bin/team-pool-client sync --once --take-over-native-store
.venv/bin/team-pool-client service-install
.venv/bin/team-pool-client status
python3 scripts/install_team_status.py install
```

Reopen Desktop and resume an existing local Code conversation. Verify a harmless
normal-speed turn. A running service alone does not prove Desktop interoperability.
The separate [Claude Swap menu bar](team-status-menu.md) shows shared-pool status
without accessing credentials or running a second account selector.
Use client 0.1.0a5 or newer; earlier pilot clients could repeatedly undo Keychain
approval. See [Keychain recovery and writer limits](keychain-access.md).

## Contribute an account from any enrolled Mac

```sh
.venv/bin/team-pool-client contribute-account
```

Sign into the intended subscription account on the official Claude page. Paste
the resulting `code#state` into the terminal's hidden prompt. The coordinator owns
the resulting refresh credentials, and the account joins the common pool.
This flow does not read or replace the Mac's current native login. Only contribute
accounts authorized for this group's use; account quota and resource permissions
are shared. It does not create per-person budgets or extra subscription capacity.

If the submission result is unclear, use `contribute-account --status`; do not
resubmit a possibly consumed code. `--cancel` cancels an unfinished login. Full
recovery behavior is in the [pilot runbook](team-pilot.md).

Existing personal `cswap` credentials are not uploaded automatically. A deliberate
transfer must stop every other refresh writer and replace refresh-capable local
material. After the server rotates a token, an old local backup may no longer be
usable; rollback must use current authoritative credentials or a fresh login.

## Operations and limits

Use `cloudflare_pool_admin.py revoke DEVICE_ID` to stop further deliveries to a
device. Lists expose only operational metadata, never tokens. The explicit
`import-account` command is reserved for credentials whose refresh ownership has
already been transferred; teammates normally use browser contribution.

The Worker is deliberately a small shared pool. Provider polling and refreshes
are serialized; large pools or slow provider responses can exceed the client's
75-second timeout. Keep the initial rollout to two Macs and a small account pool
until live acceptance passes. No claims are made that changing accounts will
automatically retry a failed turn or an already running subagent.

For normal code deployments, retain the existing Worker secrets and Durable Object
identity. Do not restore an old database snapshot containing consumed refresh
tokens; quarantine uncertain accounts and authorize fresh lineages instead.

To stop synchronization and restore the Mac's displaced personal login, quit
Desktop and use:

```sh
.venv/bin/team-pool-client service-remove
.venv/bin/team-pool-client leave --restore
```

Only restart a personal `cswap` selector after the team service has stopped and
the original native state has been restored. Re-enable its LaunchAgent with
`launchctl enable "gui/$(id -u)/com.cswap.menubar"` before loading it again.
Keep the original Desktop profile.
