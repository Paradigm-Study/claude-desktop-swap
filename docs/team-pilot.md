# Two-Mac native subscription pilot

This is an experimental implementation of the [team-pool design](team-pool-design.md),
not a validated production service. The coordinator exchanges credentials only.
Claude Desktop Code sends inference directly to Anthropic. No gateway, API-key
billing, or Fast-mode enablement is introduced.

The automated integration test connects two synthetic Mac clients to a real
local HTTPS server with fake provider credentials. Real Desktop login, token
renewal, and session continuity still require the two-Mac acceptance test below.

## Pilot boundaries

Use two controlled Macs and one or two dedicated, authorized test accounts.
The coordinator can run as a dedicated user on a Mac or Linux host. Every
credential lineage must have exactly one refresh owner: the coordinator.
Do not import the existing everyday account pool while its Claude or cswap
processes still use refresh credentials. This pilot does not migrate that pool.

The enrolled Mac receives a real reusable Anthropic access token. Device
revocation stops future deliveries; it does not invalidate a provider token
already delivered. The same native credential store can affect terminal Claude
Code. Account resource permissions and quota remain shared. See the design's
trust boundary and credential-use restrictions before provisioning accounts.

The coordinator is deliberately small: one process, local administration, no
web dashboard, no administrator network endpoint. Stop it before running admin
commands, then restart it. Clients retain their last token while it is valid.

## 1. Install the pilot package

On the coordinator and both Macs, use a separate checkout of this pilot branch:

```sh
git clone --single-branch --branch codex/team-pool \
  https://github.com/Paradigm-Study/claude-desktop-swap.git claude-desktop-swap-pilot
cd claude-desktop-swap-pilot
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python .
```

Use the absolute path to this environment for services and keep it in place.
Install the normal [Desktop launcher](../README.md#2-install-the-desktop-integration)
on each Mac while Desktop is closed. Keep each Mac's original Desktop login.
The launcher setup is separate from the package and still requires `sudo`.

The team client checks that the selected Desktop profile enables the launcher,
the expected wrapper exists, and the cswap menu-bar selector is not loaded.
Stop any foreground `cswap auto` loops as well. If the existing menu-bar
LaunchAgent is installed and loaded, unload it before enrolling:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.cswap.menubar.plist"
```

Do not delete saved accounts or log out of a shared provider account to do this.
Keep personal settings, project histories, and the Desktop login in place.

## 2. Provision a dedicated coordinator-owned account

In a **dedicated provisioning OS user**, sign into the unmodified native Claude
Code CLI using Anthropic's normal login flow. This OS user must not have the
everyday account pool or an active cswap auto-selector. Exit its Claude Code and
cswap processes after login and keep them stopped once ownership is handed off.

On that provisioning Mac, explicitly export only the current test account:

```sh
mkdir -p "$HOME/pilot-private"
chmod 700 "$HOME/pilot-private"
.venv/bin/team-pool-prepare-account \
  --from-macos-keychain --exclusive-owner \
  --out "$HOME/pilot-private/account-a.json"
```

This command reads the selected user's default native Keychain credential and
global account metadata. It exports an allowlist into a new `0600` file; it does
not copy trusted-device tokens, connector credentials, other accounts, or
conversations. It may trigger a Keychain permission prompt. It does **not** stop
other processes or erase the source; `--exclusive-owner` is your declaration
that the old lineage will have no other writer.

For a Linux provisioning user whose native CLI stores file credentials, use:

```sh
.venv/bin/team-pool-prepare-account \
  --native-credentials "$HOME/.claude/.credentials.json" \
  --native-config "$HOME/.claude.json" --exclusive-owner \
  --out "$HOME/pilot-private/account-a.json"
```

Both source files must be owned by the current user and mode `0600`. The export
requires real expiry, inference scope, subscription metadata, and account/org
IDs; it refuses incomplete data rather than inventing it. Alternate native
profiles/config directories are outside this pilot exporter.

If the coordinator is another host/user, transfer this private file using your
existing encrypted administrative channel; never put it in this repository or
send it as a teammate invitation. Import it as the coordinator OS user:

```sh
.venv/bin/team-pool init
.venv/bin/team-pool add-account --id account-a \
  --credentials "$HOME/pilot-private/account-a.json" --exclusive-owner
```

After successful import, remove that plaintext transfer file. The coordinator
stores encrypted account material in its private state directory
`~/.local/share/claude-desktop-team-pool/`. Its `master.key` must be backed up with
the database through a protected channel; anyone holding both can decrypt the
pool. Local filesystem permissions remain part of the security boundary.

For account-switch testing, repeat with a second dedicated test account. Do not
resume a provisioning CLI with old refresh material after the coordinator has
rotated it. The coordinator verifies UUID/org against the provider's profile
endpoint before delivering an account. Subscription tier strings originate in
the native login export; they are not independently verified by that endpoint.

## 3. Create two invitations and start HTTPS

While the coordinator is stopped:

```sh
.venv/bin/team-pool invite --out "$HOME/pilot-private/mac-a.invite" --ttl 3600
.venv/bin/team-pool invite --out "$HOME/pilot-private/mac-b.invite" --ttl 3600
```

Each file contains a separate, expiring, single-use invitation. Give each
teammate only their own invitation through a private channel. Redemption creates
a unique device bearer stored in that Mac's Keychain; the server retains its hash.

Use an HTTPS hostname reachable from both Macs, on a trusted private network.
Supply a certificate whose hostname matches and whose issuer both clients trust:

```sh
.venv/bin/team-pool serve --host 0.0.0.0 --port 8741 \
  --tls-cert /absolute/path/fullchain.pem \
  --tls-key /absolute/path/private-key.pem
```

Restrict network access to the two pilot Macs. There is no automatic certificate
provisioning or internet-facing hardening. A self-signed certificate is not
accepted merely because the server uses it; establish its trust separately.
The client has no skip-verification switch. Plain HTTP is allowed only on
loopback with an explicit client flag, for local tests; it is not the two-Mac path.

The coordinator polls quota on assignment requests, at most every 120 seconds
per healthy account. HTTP 429 and transport failures back off. Readings older
than 300 seconds cannot attract a new assignment. Existing clients can retain a
valid token while reporting stale or exhausted quota. Account selection is
sticky and considers active device assignments; it is not per-person accounting.

## 4. Enroll each Mac once

Close Desktop for initial takeover. Run as the teammate's normal macOS user,
**without sudo**. Replace the example URL with the verified coordinator origin:

```sh
.venv/bin/team-pool-client enroll \
  --server https://pool.internal.example.com:8741 --device-name "Pilot Mac A"
```

Enter the invitation at the hidden prompt. Alternatively use
`--invitation-file /absolute/path/mac-a.invite`; it must be a private, user-owned
file. Do not put the token itself on the command line.

Complete a deliberate first takeover, then install the background agent:

```sh
.venv/bin/team-pool-client sync --once --take-over-native-store
.venv/bin/team-pool-client status
.venv/bin/team-pool-client service-install
```

The first sync stores displaced native account data in a separate Keychain
backup. Ordinary project settings and local MCP/plugin secrets are retained.
The client config under `~/.config/claude-desktop-swap/team.json` contains device
IDs, timestamps, status, and hashes, not provider/device tokens. A pending-write
journal records expected hashes so a save failure does not destroy recovery.

Reopen Desktop and resume a local Code conversation. Initial identity/quota
checks or a cold refresh can take tens of seconds. A running service or `active`
status proves credential synchronization only; verify a real Code turn below.

## 5. Record the two-Mac acceptance results

Record results in [the acceptance worksheet](team-pilot-acceptance.md); do not
record actual account emails, tokens, prompt contents, or private session IDs.

1. On both Macs, verify an existing local Code conversation remains present.
   Run a harmless turn with normal-speed inference. Record the Desktop/bundled
   Code versions and observed subscription identity.
2. Observe a real provider-token refresh and another successful turn on both
   Macs. Confirm only the coordinator submitted the refresh grant. Shortening a
   local timestamp or using synthetic tests is not proof of real token renewal.
3. Exercise a quota-driven switch when a dedicated test account genuinely reaches
   the threshold; don't burn quota solely to force it. Record cache delay and any
   failed turn requiring a retry. Automated tests cover the threshold meanwhile.
4. Test sleep/wake and a temporary coordinator outage. A valid existing credential
   can keep working; an expired one must not silently fall back to another billing
   path. Verify recovery after reconnection.
5. Test a manual native account change on one Mac. The agent should pause and
   preserve it. Do not use `/logout` casually: provider revocation can affect
   other Macs holding the same token.
6. Revoke one device and confirm delivery stops while the other continues.
   Record the residual validity of the previously delivered provider token.
7. Record model selection, artifact/connector access, local tools, and any subagent
   behavior. Fast mode has separate billing; this pilot does not enable or fund it.

If current Desktop requires a refresh or trusted-device token on each Mac,
stop the pilot and revisit the architecture. Do not work around that result by
distributing the full account store.

## Administration and recovery

Stop the coordinator with Ctrl-C before these local commands, then restart it:

```sh
.venv/bin/team-pool accounts
.venv/bin/team-pool devices
.venv/bin/team-pool revoke DEVICE_ID
```

Revocation prevents future delivery; it cannot retroactively remove an already
delivered token. No remote admin key is distributed to clients.

An interrupted, ambiguous, or rejected refresh quarantines the account rather
than replaying a potentially consumed refresh grant. Provision a fresh, exclusively
owned lineage of that same account, then replace only its quarantined entry:

```sh
.venv/bin/team-pool add-account --id account-a \
  --credentials "$HOME/pilot-private/fresh-account-a.json" \
  --exclusive-owner --replace-quarantined
```

Changing Desktop profiles, policies, or native credentials pauses the client.
For a repaired setup/policy with unchanged native login, use `resume`, then
`sync --once`. If the LaunchAgent stopped, remove and reinstall that service.
Resume cannot overwrite an unrelated manual login. A partially completed native
write requires explicit recovery; preserve the journal and Keychain backup.

## Leave and restore

Close Desktop. To stop enrollment while preserving the current native credential:

```sh
.venv/bin/team-pool-client leave
```

To also restore the displaced native account, when it has not been changed by
another writer:

```sh
.venv/bin/team-pool-client leave --restore
```

Leaving removes the LaunchAgent and device bearer. Without `--restore`, local
recovery state and the Keychain backup remain, allowing a later explicit restore.
Restore refuses unrelated credential drift. Preserved old credentials can have
expired or been revoked and may need a normal native login afterward.
Have the administrator revoke the device on the coordinator as well.

The Desktop wrapper has its own uninstaller:

```sh
sudo python3 scripts/desktop_swap.py uninstall --user "$USER"
```

Reopen Desktop after restoration. If you return to a personal cswap setup,
re-enable its selector only after the team agent has stopped and the native
account state is appropriate. Never erase credential backups to make a conflict
message disappear without first establishing which login should survive.

## Known limits

- Desktop compatibility is experimental. No actual two-Mac result is claimed by
  the automated tests. Already-running retry clients/subagents can retain old auth.
- The native adapter checks known user/system policy sources. It refuses several
  unsupported configurations rather than bypassing them. Project-local and
  server-delivered policy must be reviewed for the chosen pilot projects; the
  background agent cannot infer all project contexts Desktop will launch.
- No fallback-file credential storage, alternative native config directory, API
  key, or provider gateway is supported by the Mac agent.
- Native advisory locks coordinate cooperative processes. A crashed lock is not
  automatically stolen; inspect its owner before recovery. A writer ignoring those
  locks can still cause a pause. Keychain permissions may prompt on a real Mac.
- The pilot cannot enforce per-person inference budgets or revoke a copied provider
  bearer independently of its actual upstream expiry. Keep the deployment limited
  to trusted test devices.

## Offline tests

```sh
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m unittest discover -s tests -p 'test_team_*.py' -v
```

The TLS test generates its own temporary certificate and fake Keychains. It
exercises two enrollments, native-store writes, quota rotation, one refresh per
account generation, manual-login pause, revocation, and explicit restoration.
All provider calls are faked; no subscription quota is consumed.
